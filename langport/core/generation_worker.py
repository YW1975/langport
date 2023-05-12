import argparse
import asyncio
import dataclasses
import logging
import json
import os
import time
from typing import List, Optional, Union
import threading
import uuid

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
import requests
from tenacity import retry, stop_after_attempt
from langport.core.base_worker import BaseModelWorker

from langport.protocol.worker_protocol import (
    BaseWorkerResult,
    EmbeddingWorkerResult,
    EmbeddingsTask,
    GenerationTask,
    GenerationWorkerResult,
    RegisterWorkerRequest,
    RemoveWorkerRequest,
    UsageInfo,
    WorkerHeartbeat,
    WorkerStatus,
)

import torch

from transformers import PreTrainedModel, PreTrainedTokenizerBase
from transformers.generation.stopping_criteria import (
    StoppingCriteria,
    StoppingCriteriaList,
    MaxLengthCriteria,
    MaxNewTokensCriteria,
)
from transformers.generation.logits_process import (
    LogitsProcessor,
    LogitsProcessorList,
    TemperatureLogitsWarper,
    RepetitionPenaltyLogitsProcessor,
    TopPLogitsWarper,
    TopKLogitsWarper,
)
from langport.constants import (
    WORKER_API_TIMEOUT,
    WORKER_HEART_BEAT_INTERVAL,
    WORKER_HEART_BEAT_CHECK_INTERVAL,
    WORKER_INFERENCE_TIMER_INTERVAL,
    ErrorCode,
)
from langport.model.model_adapter import load_model
from langport.utils import server_error_msg, pretty_print_semaphore


def prepare_logits_processor(
    temperature: float, repetition_penalty: float, top_p: float, top_k: int
) -> LogitsProcessorList:
    processor_list = LogitsProcessorList()
    # TemperatureLogitsWarper doesn't accept 0.0, 1.0 makes it a no-op so we skip two cases.
    if temperature >= 1e-5 and temperature != 1.0:
        processor_list.append(TemperatureLogitsWarper(temperature))
    if repetition_penalty > 1.0:
        processor_list.append(RepetitionPenaltyLogitsProcessor(repetition_penalty))
    if 1e-8 <= top_p < 1.0:
        processor_list.append(TopPLogitsWarper(top_p))
    if top_k > 0:
        processor_list.append(TopKLogitsWarper(top_k))
    return processor_list


def batch_generation(
    model: PreTrainedModel,
    tokenizer: PreTrainedTokenizerBase,
    device: str,
    stream_interval: int,
    tasks: List[GenerationTask],
):
    batch_size = len(tasks)
    if batch_size == 0:
        return

    prompts = [task.prompt for task in tasks]
    max_new_tokens = max([task.max_new_tokens for task in tasks])

    # init logits_processor
    logits_processor_list = []
    for task in tasks:
        logits_processor = prepare_logits_processor(
            task.temperature, task.repetition_penalty, task.top_p, task.top_k
        )
        logits_processor_list.append(logits_processor)

    # prepare init inputs
    inputs = tokenizer(
        prompts, padding="longest", return_tensors="pt", return_length=True
    )
    full_input_ids = inputs.input_ids.to(device)
    length = inputs.length
    decoder_input_ids = torch.full(
        size=(batch_size, 1),
        fill_value=model.generation_config.decoder_start_token_id,
        dtype=torch.long,
        device=device,
    )

    input_ids = full_input_ids[:, : min(length)]
    encoder_outputs = model.encoder(input_ids=full_input_ids)
    past_key_values = None

    # decode state
    is_stop = [False] * batch_size

    # step by step
    for step in range(max_new_tokens):
        if model.config.is_encoder_decoder:
            out = model(
                input_ids=input_ids,
                use_cache=True,
                encoder_outputs=encoder_outputs,
                decoder_input_ids=decoder_input_ids,
                past_key_values=past_key_values,
            )
            logits = out.logits
            past_key_values = out.past_key_values
        else:
            out = model(
                input_ids=input_ids,
                use_cache=True,
                past_key_values=past_key_values,
            )
            logits = out.logits
            past_key_values = out.past_key_values

        new_ids = []
        current_len = input_ids.shape[1]
        for i in range(batch_size):
            task = tasks[i]
            last_token_logits = logits[i][-1]

            logits_processor = logits_processor_list[i]
            if logits_processor:
                if task.repetition_penalty > 1.0:
                    tmp_output_ids = input_ids[i, :].unsqueeze(0)
                else:
                    tmp_output_ids = None
                last_token_logits = logits_processor(tmp_output_ids, logits[:, -1, :])[
                    0
                ]
            else:
                last_token_logits = logits[0, -1, :]

            if device == "mps":
                # Switch to CPU by avoiding some bugs in mps backend.
                last_token_logits = last_token_logits.float().to("cpu")

            if task.temperature < 1e-5 or task.top_p < 1e-8:  # greedy
                token = int(torch.argmax(last_token_logits))
            else:
                probs = torch.softmax(last_token_logits, dim=-1)
                token = int(torch.multinomial(probs, num_samples=1))

            if current_len < length[i]:
                new_ids.append(full_input_ids[i, current_len])
            else:
                new_ids.append(token)

            if token == tokenizer.eos_token_id:
                is_stop[i] = True
            else:
                is_stop[i] = False

            if step % stream_interval == 0 or step == max_new_tokens - 1 or is_stop[i]:
                output = tokenizer.decode(new_ids, skip_special_tokens=True)
                yield GenerationWorkerResult(
                    task_id=task.task_id,
                    type="data",
                    text=output,
                    usage=UsageInfo(
                        prompt_tokens=length[i],
                        total_tokens=length[i] + step,
                        completion_tokens=step,
                    ),
                )

            if is_stop[i]:
                yield BaseWorkerResult(task_id=task.task_id, type="done")

        input_ids = torch.cat(
            (input_ids, torch.tensor(new_ids, dtype=torch.long, device=input_ids.device)),
            dim=1,
        )

    del past_key_values


def inference_generation(worker: "GenerationModelWorker"):
    if not worker.online:
        return
    tasks = worker.fetch_tasks()
    batch_size = len(tasks)
    if batch_size == 0:
        return

    for chunk in batch_generation(
        worker.model_holder.model,
        worker.model_holder.tokenizer,
        worker.device,
        worker.stream_interval,
        tasks,
    ):
        worker.push_task_result(chunk.task_id, chunk)


class GenerationModelWorker(BaseModelWorker):
    def __init__(
        self,
        controller_addr: str,
        worker_addr: str,
        worker_id: str,
        worker_type: str,
        model_path: str,
        model_name: str,
        device: str,
        num_gpus: int,
        max_gpu_memory,
        load_8bit: bool,
        cpu_offloading: bool,
        limit_model_concurrency: int,
        max_batch: int,
        stream_interval: int,
        logger,
    ):
        super(GenerationModelWorker, self).__init__(
            controller_addr=controller_addr,
            worker_addr=worker_addr,
            worker_id=worker_id,
            worker_type=worker_type,
            model_path=model_path,
            model_name=model_name,
            device=device,
            num_gpus=num_gpus,
            max_gpu_memory=max_gpu_memory,
            load_8bit=load_8bit,
            cpu_offloading=cpu_offloading,
            limit_model_concurrency=limit_model_concurrency,
            max_batch=max_batch,
            stream_interval=stream_interval,
            logger=logger,
        )
        self.add_timer("generation_inference", 0.5, inference_generation)

    async def generation_stream(self, task: GenerationTask):
        self.add_task(task)
        async for chunk in self.fetch_task_result(task.task_id):
            yield chunk
