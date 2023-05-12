import argparse
import asyncio
import dataclasses
import logging
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Union
import threading
import uuid

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
import requests
from tenacity import retry, stop_after_attempt

from langport.protocol.worker_protocol import (
    BaseWorkerResult,
    BaseWorkerTask,
    RegisterWorkerRequest,
    RemoveWorkerRequest,
    WorkerHeartbeat,
    WorkerStatus,
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
from langport.utils.interval_timer import IntervalTimer


class WorkerHeartBeat(object):
    def __init__(self) -> None:
        self.last_time = time.time()
    
    def __call__(self, controller: "BaseModelWorker") -> Any:
        if not controller.online:
            return

        now_time = time.time()
        if now_time - self.last_time > WORKER_HEART_BEAT_INTERVAL:
            try:
                controller.send_heart_beat()
            except requests.exceptions.RequestException as e:
                controller.logger.error(f"heart beat error: {e}")
            self.last_time = now_time

class BaseModelWorker(object):
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
        self.controller_addr = controller_addr
        self.worker_addr = worker_addr
        self.worker_id = worker_id
        self.worker_type = worker_type
        if model_path.endswith("/"):
            model_path = model_path[:-1]
        self.model_name = model_name or model_path.split("/")[-1]
        self.device = device
        self.limit_model_concurrency = limit_model_concurrency
        self.stream_interval = stream_interval
        self.logger = logger
        self.max_batch_size = max_batch

        self.global_counter = 0
        self.model_semaphore = None

        self.logger.info(
            f"Loading the model {self.model_name} on worker {worker_id} ..."
        )
        self.model, self.tokenizer = load_model(
            model_path, device, num_gpus, max_gpu_memory, load_8bit, cpu_offloading
        )

        if hasattr(self.model.config, "max_sequence_length"):
            self.context_len = self.model.config.max_sequence_length
        elif hasattr(self.model.config, "max_position_embeddings"):
            self.context_len = self.model.config.max_position_embeddings
        else:
            self.context_len = 2048

        self.task_queue: List[BaseWorkerTask] = []
        self.task_output: Dict[str, List[BaseWorkerResult]] = {}

        self.timers: Dict[str, threading.Timer] = {}

        self.online = False

    def start(self):
        if self.online:
            return

        self.add_timer("heartbeat", WORKER_HEART_BEAT_CHECK_INTERVAL, WorkerHeartBeat())
        self.register_to_controller()
        self.online = True

    def stop(self):
        if not self.online:
            return

        for name, timer in self.timers.items():
            timer.cancel()
        self.timers.clear()
        self.remove_from_controller()
        self.online = False

    def add_timer(self, name: str, interval: float, fn: Callable[["BaseModelWorker"], None]) -> bool:
        if name in self.timers:
            return False
        new_timer = IntervalTimer(interval=interval, fn=fn, args=(self,))
        self.timers[name] = new_timer
        new_timer.start()
        return True

    def remove_timer(self, name: str) -> bool:
        if name not in self.timers:
            return False
        self.timers[name].cancel()
        del self.timers[name]
        return True

    def register_to_controller(self):
        self.logger.info("Register to controller")

        url = self.controller_addr + "/register_worker"
        data = RegisterWorkerRequest(
            worker_id=self.worker_id,
            worker_addr=self.worker_addr,
            worker_type=self.worker_type,
            check_heart_beat=True,
            worker_status=WorkerStatus(
                model_name=self.model_name,
                speed=1,
                queue_length=self.get_queue_length(),
            ),
        )
        r = requests.post(url, json=data.dict(), timeout=WORKER_API_TIMEOUT)
        assert r.status_code == 200

    def remove_from_controller(self):
        self.logger.info("Remove to controller")

        url = self.controller_addr + "/remove_worker"
        data = RemoveWorkerRequest(worker_id=self.worker_id)
        r = requests.post(url, json=data.dict(), timeout=WORKER_API_TIMEOUT)
        assert r.status_code == 200

    @retry(stop=stop_after_attempt(5))
    def send_heart_beat(self):
        self.logger.info(
            f"Send heart beat. Models: {[self.model_name]}. "
            f"Semaphore: {pretty_print_semaphore(self.model_semaphore)}. "
            f"global_counter: {self.global_counter}"
        )

        url = self.controller_addr + "/receive_heart_beat"

        ret = requests.post(
            url,
            json=WorkerHeartbeat(
                worker_id=self.worker_id,
                status=WorkerStatus(
                    model_name=self.model_name,
                    speed=1,
                    queue_length=self.get_queue_length(),
                ),
            ),
            timeout=WORKER_API_TIMEOUT,
        )
        exist = ret.json()["exist"]

        if not exist:
            self.register_to_controller()

    def get_queue_length(self):
        if (
            self.model_semaphore is None
            or self.model_semaphore._value is None
            or self.model_semaphore._waiters is None
        ):
            return 0
        else:
            return (
                self.limit_model_concurrency
                - self.model_semaphore._value
                + len(self.model_semaphore._waiters)
            )

    def get_status(self) -> WorkerStatus:
        return WorkerStatus(
            worker_id=self.worker_id,
            model_name=self.model_name,
            speed=1,
            queue_length=self.get_queue_length(),
        )
    
    def add_task(self, task: BaseWorkerTask):
        self.task_queue.append(task)
        self.task_output[task.task_id] = []
    
    async def fetch_task_result(self, task_id: str):
        out_queue = self.task_output[task_id]
        while True:
            if len(out_queue) <= 0:
                await asyncio.sleep(0.1)
                continue
            event = out_queue.pop(0)
            if event.type == "done":
                break
            elif event.type == "error":
                yield event
                break
            else:
                yield event
            await asyncio.sleep(0.1)
        
        del self.task_output[task_id]
    
    def fetch_tasks(self) -> List[BaseWorkerResult]:
        task_batch = []
        while len(task_batch) <= self.max_batch_size and len(self.task_queue) > 0:
            task = self.task_queue.pop(0)
            task_batch.append(task)
        return task_batch
    
    def push_task_result(self, task_id: str, response: BaseWorkerResult):
        self.task_output[task_id].append(response)