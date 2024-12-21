# -*- coding: utf-8 -*-
"""
Copyright (C) 2024 Xiaomi Corporation.

The ownership and intellectual property rights of Xiaomi Home Assistant
Integration and related Xiaomi cloud service API interface provided under this
license, including source code and object code (collectively, "Licensed Work"),
are owned by Xiaomi. Subject to the terms and conditions of this License, Xiaomi
hereby grants you a personal, limited, non-exclusive, non-transferable,
non-sublicensable, and royalty-free license to reproduce, use, modify, and
distribute the Licensed Work only for your use of Home Assistant for
non-commercial purposes. For the avoidance of doubt, Xiaomi does not authorize
you to use the Licensed Work for any other purpose, including but not limited
to use Licensed Work to develop applications (APP), Web services, and other
forms of software.

You may reproduce and distribute copies of the Licensed Work, with or without
modifications, whether in source or object form, provided that you must give
any other recipients of the Licensed Work a copy of this License and retain all
copyright and disclaimers.

Xiaomi provides the Licensed Work on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied, including, without
limitation, any warranties, undertakes, or conditions of TITLE, NO ERROR OR
OMISSION, CONTINUITY, RELIABILITY, NON-INFRINGEMENT, MERCHANTABILITY, or
FITNESS FOR A PARTICULAR PURPOSE. In any event, you are solely responsible
for any direct, indirect, special, incidental, or consequential damages or
losses arising from the use or inability to use the Licensed Work.

Xiaomi reserves all rights not expressly granted to you in this License.
Except for the rights expressly granted by Xiaomi under this License, Xiaomi
does not authorize you in any form to use the trademarks, copyrights, or other
forms of intellectual property rights of Xiaomi and its affiliates, including,
without limitation, without obtaining other written permission from Xiaomi, you
shall not use "Xiaomi", "Mijia" and other words related to Xiaomi or words that
may make the public associate with Xiaomi in any form to publicize or promote
the software or hardware devices that use the Licensed Work.

Xiaomi has the right to immediately terminate all your authorization under this
License in the event:
1. You assert patent invalidation, litigation, or other claims against patents
or other intellectual property rights of Xiaomi or its affiliates; or,
2. You make, have made, manufacture, sell, or offer to sell products that knock
off Xiaomi or its affiliates' products.

MIoT Pub/Sub client.
"""
import asyncio
import json
import logging
import os
import queue
import random
import re
import ssl
import struct
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Optional, final

from paho.mqtt.client import (
    MQTT_ERR_SUCCESS,
    MQTT_ERR_UNKNOWN,
    Client,
    MQTTv5)

# pylint: disable=relative-beyond-top-level
from .common import MIoTMatcher
from .const import MIHOME_MQTT_KEEPALIVE
from .miot_error import MIoTErrorCode, MIoTMipsError
from .miot_ev import MIoTEventLoop, TimeoutHandle

_LOGGER = logging.getLogger(__name__)


class MipsMsgTypeOptions(Enum):
    """MIoT Pub/Sub message type."""
    ID = 0
    RET_TOPIC = auto()
    PAYLOAD = auto()
    FROM = auto()
    MAX = auto()


class MipsMessage:
    """MIoT Pub/Sub message."""
    mid: int = 0
    msg_from: str = None
    ret_topic: str = None
    payload: str = None

    @staticmethod
    def unpack(data: bytes):
        mips_msg = MipsMessage()
        data_len = len(data)
        data_start = 0
        data_end = 0
        while data_start < data_len:
            data_end = data_start+5
            unpack_len, unpack_type = struct.unpack(
                '<IB', data[data_start:data_end])
            unpack_data = data[data_end:data_end+unpack_len]
            #  string end with \x00
            match unpack_type:
                case MipsMsgTypeOptions.ID.value:
                    mips_msg.mid = int.from_bytes(
                        unpack_data, byteorder='little')
                case MipsMsgTypeOptions.RET_TOPIC.value:
                    mips_msg.ret_topic = str(
                        unpack_data.strip(b'\x00'), 'utf-8')
                case MipsMsgTypeOptions.PAYLOAD.value:
                    mips_msg.payload = str(unpack_data.strip(b'\x00'), 'utf-8')
                case MipsMsgTypeOptions.FROM.value:
                    mips_msg.msg_from = str(
                        unpack_data.strip(b'\x00'), 'utf-8')
                case _:
                    pass
            data_start = data_end+unpack_len
        return mips_msg

    @staticmethod
    def pack(
        mid: int, payload: str, msg_from: str = None, ret_topic: str = None
    ) -> bytes:
        if mid is None or payload is None:
            raise MIoTMipsError('invalid mid or payload')
        pack_msg: bytes = b''
        # mid
        pack_msg += struct.pack('<IBI', 4, MipsMsgTypeOptions.ID.value, mid)
        # msg_from
        if msg_from:
            pack_len = len(msg_from)
            pack_msg += struct.pack(
                f'<IB{pack_len}sx', pack_len+1,
                MipsMsgTypeOptions.FROM.value, msg_from.encode('utf-8'))
        # ret_topic
        if ret_topic:
            pack_len = len(ret_topic)
            pack_msg += struct.pack(
                f'<IB{pack_len}sx', pack_len+1,
                MipsMsgTypeOptions.RET_TOPIC.value, ret_topic.encode('utf-8'))
        # payload
        pack_len = len(payload)
        pack_msg += struct.pack(
            f'<IB{pack_len}sx', pack_len+1,
            MipsMsgTypeOptions.PAYLOAD.value, payload.encode('utf-8'))
        return pack_msg

    def __str__(self) -> str:
        return f'{self.mid}, {self.msg_from}, {self.ret_topic}, {self.payload}'


class MipsCmdType(Enum):
    """MIoT Pub/Sub command type."""
    CONNECT = 0
    DISCONNECT = auto()
    DEINIT = auto()
    SUB = auto()
    UNSUB = auto()
    CALL_API = auto()
    REG_BROADCAST = auto()
    UNREG_BROADCAST = auto()

    REG_MIPS_STATE = auto()
    UNREG_MIPS_STATE = auto()
    REG_DEVICE_STATE = auto()
    UNREG_DEVICE_STATE = auto()


@dataclass
class MipsCmd:
    """MIoT Pub/Sub command."""
    type_: MipsCmdType
    data: Any

    def __init__(self, type_: MipsCmdType, data: Any) -> None:
        self.type_ = type_
        self.data = data


@dataclass
class MipsRequest:
    """MIoT Pub/Sub request."""
    mid: int = None
    on_reply: Callable[[str, Any], None] = None
    on_reply_ctx: Any = None
    timer: TimeoutHandle = None


@dataclass
class MipsRequestData:
    """MIoT Pub/Sub request data."""
    topic: str = None
    payload: str = None
    on_reply: Callable[[str, Any], None] = None
    on_reply_ctx: Any = None
    timeout_ms: int = None


@dataclass
class MipsSendBroadcastData:
    """MIoT Pub/Sub send broadcast data."""
    topic: str = None
    payload: str = None


@dataclass
class MipsIncomingApiCall:
    """MIoT Pub/Sub incoming API call."""
    mid: int = None
    ret_topic: str = None
    timer: TimeoutHandle = None


@dataclass
class MipsApi:
    """MIoT Pub/Sub API."""
    topic: str = None
    """
    param1: session
    param2: payload
    param3: handler_ctx
    """
    handler: Callable[[MipsIncomingApiCall, str, Any], None] = None
    handler_ctx: Any = None


class MipsRegApi(MipsApi):
    """.MIoT Pub/Sub register API."""


@dataclass
class MipsReplyData:
    """MIoT Pub/Sub reply data."""
    session: MipsIncomingApiCall = None
    payload: str = None


@dataclass
class MipsBroadcast:
    """MIoT Pub/Sub broadcast."""
    topic: str = None
    """
    param 1: msg topic
    param 2: msg payload
    param 3: handle_ctx
    """
    handler: Callable[[str, str, Any], None] = None
    handler_ctx: Any = None

    def __str__(self) -> str:
        return f'{self.topic}, {id(self.handler)}, {id(self.handler_ctx)}'


class MipsRegBroadcast(MipsBroadcast):
    """MIoT Pub/Sub register broadcast."""


@dataclass
class MipsState:
    """MIoT Pub/Sub state."""
    key: str = None
    """
    str: key
    bool: mips connect state
    """
    handler: Callable[[str, bool], asyncio.Future] = None


class MipsRegState(MipsState):
    """MIoT Pub/Sub register state."""


class MIoTDeviceState(Enum):
    """MIoT device state define."""
    DISABLE = 0
    OFFLINE = auto()
    ONLINE = auto()


@dataclass
class MipsDeviceState:
    """MIoT Pub/Sub device state."""
    did: str = None
    """handler
    str: did
    MIoTDeviceState: online/offline/disable
    Any: ctx
    """
    handler: Callable[[str, MIoTDeviceState, Any], None] = None
    handler_ctx: Any = None


class MipsRegDeviceState(MipsDeviceState):
    """MIoT Pub/Sub register device state."""


class MipsClient(ABC):
    """MIoT Pub/Sub client."""
    # pylint: disable=unused-argument
    MQTT_INTERVAL_MS = 1000
    MIPS_QOS: int = 2
    UINT32_MAX: int = 0xFFFFFFFF
    MIPS_RECONNECT_INTERVAL_MIN: int = 30000
    MIPS_RECONNECT_INTERVAL_MAX: int = 600000
    MIPS_SUB_PATCH: int = 300
    MIPS_SUB_INTERVAL: int = 1000
    main_loop: asyncio.AbstractEventLoop
    _logger: logging.Logger
    _client_id: str
    _host: str
    _port: int
    _username: str
    _password: str
    _ca_file: str
    _cert_file: str
    _key_file: str

    _mqtt_logger: logging.Logger
    _mqtt: Client
    _mqtt_fd: int
    _mqtt_timer: TimeoutHandle
    _mqtt_state: bool

    _event_connect: asyncio.Event
    _event_disconnect: asyncio.Event
    _mev: MIoTEventLoop
    _mips_thread: threading.Thread
    _mips_queue: queue.Queue
    _cmd_event_fd: os.eventfd
    _mips_reconnect_tag: bool
    _mips_reconnect_interval: int
    _mips_reconnect_timer: Optional[TimeoutHandle]
    _mips_state_sub_map: dict[str, MipsState]
    _mips_sub_pending_map: dict[str, int]
    _mips_sub_pending_timer: Optional[TimeoutHandle]

    _on_mips_cmd: Callable[[MipsCmd], None]
    _on_mips_message: Callable[[str, bytes], None]
    _on_mips_connect: Callable[[int, dict], None]
    _on_mips_disconnect: Callable[[int, dict], None]

    def __init__(
            self, client_id: str, host: str, port: int,
            username: str = None, password: str = None,
            ca_file: str = None, cert_file: str = None, key_file: str = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        # MUST run with running loop
        self.main_loop = loop or asyncio.get_running_loop()
        self._logger = None
        self._client_id = client_id
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._ca_file = ca_file
        self._cert_file = cert_file
        self._key_file = key_file

        self._mqtt_logger = None
        self._mqtt_fd = -1
        self._mqtt_timer = None
        self._mqtt_state = False
        # mqtt init for API_VERSION2,
        # callback_api_version=CallbackAPIVersion.VERSION2,
        self._mqtt = Client(client_id=self._client_id, protocol=MQTTv5)
        self._mqtt.enable_logger(logger=self._mqtt_logger)

        # Mips init
        self._event_connect = asyncio.Event()
        self._event_disconnect = asyncio.Event()
        self._mips_reconnect_tag = False
        self._mips_reconnect_interval = 0
        self._mips_reconnect_timer = None
        self._mips_state_sub_map = {}
        self._mips_sub_pending_map = {}
        self._mips_sub_pending_timer = None
        self._mev = MIoTEventLoop()
        self._mips_queue = queue.Queue()
        self._cmd_event_fd = os.eventfd(0, os.O_NONBLOCK)
        self.mev_set_read_handler(
            self._cmd_event_fd, self.__mips_cmd_read_handler, None)
        self._mips_thread = threading.Thread(target=self.__mips_loop_thread)
        self._mips_thread.daemon = True
        self._mips_thread.name = self._client_id
        self._mips_thread.start()

        self._on_mips_cmd = None
        self._on_mips_message = None
        self._on_mips_connect = None
        self._on_mips_disconnect = None

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @final
    @property
    def mips_state(self) -> bool:
        """mips connect state.

        Returns:
            bool: True: connected, False: disconnected
        """
        return self._mqtt and self._mqtt.is_connected()

    @final
    def mips_deinit(self) -> None:
        self._mips_send_cmd(type_=MipsCmdType.DEINIT, data=None)
        self._mips_thread.join()
        self._mips_thread = None

        self._logger = None
        self._client_id = None
        self._host = None
        self._port = None
        self._username = None
        self._password = None
        self._ca_file = None
        self._cert_file = None
        self._key_file = None
        self._mqtt_logger = None
        self._mips_state_sub_map = None
        self._mips_sub_pending_map = None
        self._mips_sub_pending_timer = None

        self._event_connect = None
        self._event_disconnect = None

    def update_mqtt_password(self, password: str) -> None:
        self._password = password
        self._mqtt.username_pw_set(
            username=self._username, password=self._password)

    def log_debug(self, msg, *args, **kwargs) -> None:
        if self._logger:
            self._logger.debug(f'{self._client_id}, '+msg, *args, **kwargs)

    def log_info(self, msg, *args, **kwargs) -> None:
        if self._logger:
            self._logger.info(f'{self._client_id}, '+msg, *args, **kwargs)

    def log_error(self, msg, *args, **kwargs) -> None:
        if self._logger:
            self._logger.error(f'{self._client_id}, '+msg, *args, **kwargs)

    def enable_logger(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger

    def enable_mqtt_logger(
        self, logger: Optional[logging.Logger] = None
    ) -> None:
        if logger:
            self._mqtt.enable_logger(logger=logger)
        else:
            self._mqtt.disable_logger()

    @final
    def mips_connect(self) -> None:
        """mips connect."""
        return self._mips_send_cmd(type_=MipsCmdType.CONNECT, data=None)

    @final
    async def mips_connect_async(self) -> None:
        """mips connect async."""
        self._mips_send_cmd(type_=MipsCmdType.CONNECT, data=None)
        return await self._event_connect.wait()

    @final
    def mips_disconnect(self) -> None:
        """mips disconnect."""
        return self._mips_send_cmd(type_=MipsCmdType.DISCONNECT, data=None)

    @final
    async def mips_disconnect_async(self) -> None:
        """mips disconnect async."""
        self._mips_send_cmd(type_=MipsCmdType.DISCONNECT, data=None)
        return await self._event_disconnect.wait()

    @final
    def sub_mips_state(
        self, key: str, handler: Callable[[str, bool], asyncio.Future]
    ) -> bool:
        """Subscribe mips state.
        NOTICE: callback to main loop thread
        """
        if isinstance(key, str) is False or handler is None:
            raise MIoTMipsError('invalid params')
        return self._mips_send_cmd(
            type_=MipsCmdType.REG_MIPS_STATE,
            data=MipsRegState(key=key, handler=handler))

    @final
    def unsub_mips_state(self, key: str) -> bool:
        """Unsubscribe mips state."""
        if isinstance(key, str) is False:
            raise MIoTMipsError('invalid params')
        return self._mips_send_cmd(
            type_=MipsCmdType.UNREG_MIPS_STATE, data=MipsRegState(key=key))

    @final
    def mev_set_timeout(
        self, timeout_ms: int, handler: Callable[[Any], None],
        handler_ctx: Any = None
    ) -> Optional[TimeoutHandle]:
        """set timeout.
        NOTICE: Internal function, only mips threads are allowed to call
        """
        if self._mev is None:
            return None
        return self._mev.set_timeout(
            timeout_ms=timeout_ms,  handler=handler, handler_ctx=handler_ctx)

    @final
    def mev_clear_timeout(self, handle: TimeoutHandle) -> None:
        """clear timeout.
        NOTICE: Internal function, only mips threads are allowed to call
        """
        if self._mev is None:
            return
        self._mev.clear_timeout(handle)

    @final
    def mev_set_read_handler(
        self, fd: int, handler: Callable[[Any], None], handler_ctx: Any
    ) -> bool:
        """set read handler.
        NOTICE: Internal function, only mips threads are allowed to call
        """
        if self._mev is None:
            return False
        return self._mev.set_read_handler(
            fd=fd, handler=handler, handler_ctx=handler_ctx)

    @final
    def mev_set_write_handler(
        self, fd: int, handler: Callable[[Any], None], handler_ctx: Any
    ) -> bool:
        """set write handler.
        NOTICE: Internal function, only mips threads are allowed to call
        """
        if self._mev is None:
            return False
        return self._mev.set_write_handler(
            fd=fd, handler=handler, handler_ctx=handler_ctx)

    @property
    def on_mips_cmd(self) -> Callable[[MipsCmd], None]:
        return self._on_mips_cmd

    @on_mips_cmd.setter
    def on_mips_cmd(self, handler: Callable[[MipsCmd], None]) -> None:
        """MUST set after __init__ done.
        NOTICE thread safe, this function will be called at the **mips** thread
        """
        self._on_mips_cmd = handler

    @property
    def on_mips_message(self) -> Callable[[str, bytes], None]:
        return self._on_mips_message

    @on_mips_message.setter
    def on_mips_message(self, handler: Callable[[str, bytes], None]) -> None:
        """MUST set after __init__ done.
        NOTICE thread safe, this function will be called at the **mips** thread
        """
        self._on_mips_message = handler

    @property
    def on_mips_connect(self) -> Callable[[int, dict], None]:
        return self._on_mips_connect

    @on_mips_connect.setter
    def on_mips_connect(self, handler: Callable[[int, dict], None]) -> None:
        """MUST set after __init__ done.
        NOTICE thread safe, this function will be called at the
        **main loop** thread
        """
        self._on_mips_connect = handler

    @property
    def on_mips_disconnect(self) -> Callable[[int, dict], None]:
        return self._on_mips_disconnect

    @on_mips_disconnect.setter
    def on_mips_disconnect(self, handler: Callable[[int, dict], None]) -> None:
        """MUST set after __init__ done.
        NOTICE thread safe, this function will be called at the
        **main loop** thread
        """
        self._on_mips_disconnect = handler

    @abstractmethod
    def sub_prop(
        self, did: str, handler: Callable[[dict, Any], None],
        siid: int = None, piid: int = None, handler_ctx: Any = None
    ) -> bool: ...

    @abstractmethod
    def unsub_prop(
        self, did: str, siid: int = None, piid: int = None
    ) -> bool: ...

    @abstractmethod
    def sub_event(
        self, did: str, handler: Callable[[dict, Any], None],
        siid: int = None, eiid: int = None, handler_ctx: Any = None
    ) -> bool: ...

    @abstractmethod
    def unsub_event(
        self, did: str, siid: int = None, eiid: int = None
    ) -> bool: ...

    @abstractmethod
    async def get_dev_list_async(
        self, payload: str = None, timeout_ms: int = 10000
    ) -> dict[str, dict]: ...

    @abstractmethod
    async def get_prop_async(
        self, did: str, siid: int, piid: int, timeout_ms: int = 10000
    ) -> Any: ...

    @abstractmethod
    async def set_prop_async(
        self, did: str, siid: int, piid: int, value: Any,
        timeout_ms: int = 10000
    ) -> bool: ...

    @abstractmethod
    async def action_async(
        self, did: str, siid: int, aiid: int, in_list: list,
        timeout_ms: int = 10000
    ) -> tuple[bool, list]: ...

    @final
    def _mips_sub_internal(self, topic: str) -> None:
        """mips subscribe.
        NOTICE: Internal function, only mips threads are allowed to call
        """
        self.__thread_check()
        if not self._mqtt or not self._mqtt.is_connected():
            return
        try:
            if topic not in self._mips_sub_pending_map:
                self._mips_sub_pending_map[topic] = 0
            if not self._mips_sub_pending_timer:
                self._mips_sub_pending_timer = self.mev_set_timeout(
                    10, self.__mips_sub_internal_pending_handler, topic)
        except Exception as err:  # pylint: disable=broad-exception-caught
            # Catch all exception
            self.log_error(f'mips sub internal error, {topic}. {err}')

    @final
    def _mips_unsub_internal(self, topic: str) -> None:
        """mips unsubscribe.
        NOTICE: Internal function, only mips threads are allowed to call
        """
        self.__thread_check()
        if not self._mqtt or not self._mqtt.is_connected():
            return
        try:
            result, mid = self._mqtt.unsubscribe(topic=topic)
            if result == MQTT_ERR_SUCCESS:
                self.log_debug(
                    f'mips unsub internal success, {result}, {mid}, {topic}')
                return
            self.log_error(
                f'mips unsub internal error, {result}, {mid}, {topic}')
        except Exception as err:  # pylint: disable=broad-exception-caught
            # Catch all exception
            self.log_error(f'mips unsub internal error, {topic}, {err}')

    @final
    def _mips_publish_internal(
        self, topic: str, payload: str | bytes,
        wait_for_publish: bool = False, timeout_ms: int = 10000
    ) -> bool:
        """mips publish message.
        NOTICE: Internal function, only mips threads are allowed to call

        """
        self.__thread_check()
        if not self._mqtt or not self._mqtt.is_connected():
            return False
        try:
            handle = self._mqtt.publish(
                topic=topic, payload=payload, qos=self.MIPS_QOS)
            # self.log_debug(f'_mips_publish_internal, {topic}, {payload}')
            if wait_for_publish is True:
                handle.wait_for_publish(timeout_ms/1000.0)
            return True
        except Exception as err:  # pylint: disable=broad-exception-caught
            # Catch other exception
            self.log_error(f'mips publish internal error, {err}')
        return False

    @final
    def _mips_send_cmd(self, type_: MipsCmdType, data: Any) -> bool:
        if self._mips_queue is None or self._cmd_event_fd is None:
            raise MIoTMipsError('send mips cmd disable')
        # Put data to queue
        self._mips_queue.put(MipsCmd(type_=type_, data=data))
        # Write event fd
        os.eventfd_write(self._cmd_event_fd, 1)
        # self.log_debug(f'send mips cmd, {type}, {data}')
        return True

    def __thread_check(self) -> None:
        if threading.current_thread() is not self._mips_thread:
            raise MIoTMipsError('illegal call')

    def __mips_cmd_read_handler(self, ctx: Any) -> None:
        fd_value = os.eventfd_read(self._cmd_event_fd)
        if fd_value == 0:
            return
        while self._mips_queue.empty() is False:
            mips_cmd: MipsCmd = self._mips_queue.get(block=False)
            if mips_cmd.type_ == MipsCmdType.CONNECT:
                self._mips_reconnect_tag = True
                self.__mips_try_reconnect(immediately=True)
            elif mips_cmd.type_ == MipsCmdType.DISCONNECT:
                self._mips_reconnect_tag = False
                self.__mips_disconnect()
            elif mips_cmd.type_ == MipsCmdType.DEINIT:
                self.log_info('mips client recv deinit cmd')
                self.__mips_disconnect()
                # Close cmd event fd
                if self._cmd_event_fd:
                    self.mev_set_read_handler(
                        self._cmd_event_fd, None, None)
                    os.close(self._cmd_event_fd)
                    self._cmd_event_fd = None
                if self._mips_queue:
                    self._mips_queue = None
                # ev loop stop
                if self._mev:
                    self._mev.loop_stop()
                    self._mev = None
                break
            elif mips_cmd.type_ == MipsCmdType.REG_MIPS_STATE:
                state: MipsState = mips_cmd.data
                self._mips_state_sub_map[state.key] = state
                self.log_debug(f'mips register mips state, {state.key}')
            elif mips_cmd.type_ == MipsCmdType.UNREG_MIPS_STATE:
                state: MipsState = mips_cmd.data
                del self._mips_state_sub_map[state.key]
                self.log_debug(f'mips unregister mips state, {state.key}')
            else:
                if self._on_mips_cmd:
                    self._on_mips_cmd(mips_cmd=mips_cmd)

    def __mqtt_read_handler(self, ctx: Any) -> None:
        self.__mqtt_loop_handler(ctx=ctx)

    def __mqtt_write_handler(self, ctx: Any) -> None:
        self.mev_set_write_handler(self._mqtt_fd, None, None)
        self.__mqtt_loop_handler(ctx=ctx)

    def __mqtt_timer_handler(self, ctx: Any) -> None:
        self.__mqtt_loop_handler(ctx=ctx)
        if self._mqtt:
            self._mqtt_timer = self.mev_set_timeout(
                self.MQTT_INTERVAL_MS, self.__mqtt_timer_handler, None)

    def __mqtt_loop_handler(self, ctx: Any) -> None:
        try:
            if self._mqtt:
                self._mqtt.loop_read()
            if self._mqtt:
                self._mqtt.loop_write()
            if self._mqtt:
                self._mqtt.loop_misc()
            if self._mqtt and self._mqtt.want_write():
                self.mev_set_write_handler(
                    self._mqtt_fd, self.__mqtt_write_handler, None)
        except Exception as err:  # pylint: disable=broad-exception-caught
            # Catch all exception
            self.log_error(f'__mqtt_loop_handler, {err}')
            raise err

    def __mips_loop_thread(self) -> None:
        self.log_info('mips_loop_thread start')
        # Set mqtt config
        if self._username:
            self._mqtt.username_pw_set(
                username=self._username, password=self._password)
        if (
            self._ca_file
            and self._cert_file
            and self._key_file
        ):
            self._mqtt.tls_set(
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
                ca_certs=self._ca_file,
                certfile=self._cert_file,
                keyfile=self._key_file)
        else:
            self._mqtt.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self._mqtt.tls_insecure_set(True)
        self._mqtt.on_connect = self.__on_connect
        self._mqtt.on_connect_fail = self.__on_connect_failed
        self._mqtt.on_disconnect = self.__on_disconnect
        self._mqtt.on_message = self.__on_message
        # Run event loop
        self._mev.loop_forever()
        self.log_info('mips_loop_thread exit!')

    def __on_connect(self, client, user_data, flags, rc, props) -> None:
        if not self._mqtt.is_connected():
            return
        self.log_info(f'mips connect, {flags}, {rc}, {props}')
        self._mqtt_state = True
        if self._on_mips_connect:
            self.mev_set_timeout(
                timeout_ms=0,
                handler=lambda ctx:
                    self._on_mips_connect(rc, props))
        for item in self._mips_state_sub_map.values():
            if item.handler is None:
                continue
            self.main_loop.call_soon_threadsafe(
                self.main_loop.create_task,
                item.handler(item.key, True))
        # Resolve future
        self._event_connect.set()
        self._event_disconnect.clear()

    def __on_connect_failed(self, client, user_data, flags, rc) -> None:
        self.log_error(f'mips connect failed, {flags}, {rc}')
        # Try to reconnect
        self.__mips_try_reconnect()

    def __on_disconnect(self,  client, user_data, rc, props) -> None:
        if self._mqtt_state:
            self.log_error(f'mips disconnect, {rc}, {props}')
            self._mqtt_state = False
            if self._mqtt_timer:
                self.mev_clear_timeout(self._mqtt_timer)
                self._mqtt_timer = None
            if self._mqtt_fd != -1:
                self.mev_set_read_handler(self._mqtt_fd, None, None)
                self.mev_set_write_handler(self._mqtt_fd, None, None)
                self._mqtt_fd = -1
            # Clear retry sub
            if self._mips_sub_pending_timer:
                self.mev_clear_timeout(self._mips_sub_pending_timer)
                self._mips_sub_pending_timer = None
            self._mips_sub_pending_map = {}
            if self._on_mips_disconnect:
                self.mev_set_timeout(
                    timeout_ms=0,
                    handler=lambda ctx:
                        self._on_mips_disconnect(rc, props))
            # Call state sub handler
            for item in self._mips_state_sub_map.values():
                if item.handler is None:
                    continue
                self.main_loop.call_soon_threadsafe(
                    self.main_loop.create_task,
                    item.handler(item.key, False))

        # Try to reconnect
        self.__mips_try_reconnect()
        # Set event
        self._event_disconnect.set()
        self._event_connect.clear()

    def __on_message(self, client, user_data, msg) -> None:
        self._on_mips_message(topic=msg.topic, payload=msg.payload)

    def __mips_try_reconnect(self, immediately: bool = False) -> None:
        if self._mips_reconnect_timer:
            self.mev_clear_timeout(self._mips_reconnect_timer)
            self._mips_reconnect_timer = None
        if not self._mips_reconnect_tag:
            return
        interval: int = 0
        if not immediately:
            interval = self.__get_next_reconnect_time()
            self.log_error(
                'mips try reconnect after %sms', interval)
        self._mips_reconnect_timer = self.mev_set_timeout(
            interval, self.__mips_connect, None)

    def __mips_sub_internal_pending_handler(self, ctx: Any) -> None:
        subbed_count = 1
        for topic in list(self._mips_sub_pending_map.keys()):
            if subbed_count > self.MIPS_SUB_PATCH:
                break
            count = self._mips_sub_pending_map[topic]
            if count > 3:
                self._mips_sub_pending_map.pop(topic)
                self.log_error(f'retry mips sub internal error, {topic}')
                continue
            subbed_count += 1
            result, mid = self._mqtt.subscribe(topic, qos=self.MIPS_QOS)
            if result == MQTT_ERR_SUCCESS:
                self._mips_sub_pending_map.pop(topic)
                self.log_debug(f'mips sub internal success, {topic}')
                continue
            self._mips_sub_pending_map[topic] = count+1
            self.log_error(
                f'retry mips sub internal, {count}, {topic}, {result}, {mid}')

        if len(self._mips_sub_pending_map):
            self._mips_sub_pending_timer = self.mev_set_timeout(
                self.MIPS_SUB_INTERVAL,
                self.__mips_sub_internal_pending_handler, None)
        else:
            self._mips_sub_pending_timer = None

    def __mips_connect(self, ctx: Any = None) -> None:
        result = MQTT_ERR_UNKNOWN
        if self._mips_reconnect_timer:
            self.mev_clear_timeout(self._mips_reconnect_timer)
            self._mips_reconnect_timer = None
        try:
            # Try clean mqtt fd before mqtt connect
            if self._mqtt_timer:
                self.mev_clear_timeout(self._mqtt_timer)
                self._mqtt_timer = None
            if self._mqtt_fd != -1:
                self.mev_set_read_handler(self._mqtt_fd, None, None)
                self.mev_set_write_handler(self._mqtt_fd, None, None)
                self._mqtt_fd = -1
            result = self._mqtt.connect(
                host=self._host, port=self._port,
                clean_start=True, keepalive=MIHOME_MQTT_KEEPALIVE)
            self.log_info(f'__mips_connect success, {result}')
        except (TimeoutError, OSError) as error:
            self.log_error('__mips_connect, connect error, %s', error)

        if result == MQTT_ERR_SUCCESS:
            self._mqtt_fd = self._mqtt.socket()
            self.log_debug(f'__mips_connect, _mqtt_fd, {self._mqtt_fd}')
            self.mev_set_read_handler(
                self._mqtt_fd, self.__mqtt_read_handler, None)
            if self._mqtt.want_write():
                self.mev_set_write_handler(
                    self._mqtt_fd, self.__mqtt_write_handler, None)
            self._mqtt_timer = self.mev_set_timeout(
                self.MQTT_INTERVAL_MS, self.__mqtt_timer_handler, None)
        else:
            self.log_error(f'__mips_connect error result, {result}')
            self.__mips_try_reconnect()

    def __mips_disconnect(self) -> None:
        if self._mips_reconnect_timer:
            self.mev_clear_timeout(self._mips_reconnect_timer)
            self._mips_reconnect_timer = None
        if self._mqtt_timer:
            self.mev_clear_timeout(self._mqtt_timer)
            self._mqtt_timer = None
        if self._mqtt_fd != -1:
            self.mev_set_read_handler(self._mqtt_fd, None, None)
            self.mev_set_write_handler(self._mqtt_fd, None, None)
            self._mqtt_fd = -1
        self._mqtt.disconnect()

    def __get_next_reconnect_time(self) -> int:
        if self._mips_reconnect_interval == 0:
            self._mips_reconnect_interval = self.MIPS_RECONNECT_INTERVAL_MIN
        else:
            self._mips_reconnect_interval = min(
                self._mips_reconnect_interval*2,
                self.MIPS_RECONNECT_INTERVAL_MAX)
        return self._mips_reconnect_interval


class MipsCloudClient(MipsClient):
    """MIoT Pub/Sub Cloud Client."""
    # pylint: disable=unused-argument
    # pylint: disable=inconsistent-quotes
    _msg_matcher: MIoTMatcher

    def __init__(
            self, uuid: str, cloud_server: str, app_id: str,
            token: str, port: int = 8883,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._msg_matcher = MIoTMatcher()
        super().__init__(
            client_id=f'ha.{uuid}', host=f'{cloud_server}-ha.mqtt.io.mi.com',
            port=port, username=app_id, password=token, loop=loop)

        self.on_mips_cmd = self.__on_mips_cmd_handler
        self.on_mips_message = self.__on_mips_message_handler
        self.on_mips_connect = self.__on_mips_connect_handler
        self.on_mips_disconnect = self.__on_mips_disconnect_handler

    def deinit(self) -> None:
        self.mips_deinit()
        self._msg_matcher = None
        self.on_mips_cmd = None
        self.on_mips_message = None
        self.on_mips_connect = None

    @final
    def connect(self) -> None:
        self.mips_connect()

    @final
    async def connect_async(self) -> None:
        await self.mips_connect_async()

    @final
    def disconnect(self) -> None:
        self.mips_disconnect()
        self._msg_matcher = MIoTMatcher()

    @final
    async def disconnect_async(self) -> None:
        await self.mips_disconnect_async()
        self._msg_matcher = MIoTMatcher()

    def update_access_token(self, access_token: str) -> bool:
        if not isinstance(access_token, str):
            raise MIoTMipsError('invalid token')
        return self.update_mqtt_password(password=access_token)

    @final
    def sub_prop(
        self, did: str, handler: Callable[[dict, Any], None],
        siid: int = None, piid: int = None, handler_ctx: Any = None
    ) -> bool:
        if not isinstance(did, str) or handler is None:
            raise MIoTMipsError('invalid params')

        topic: str = (
            f'device/{did}/up/properties_changed/'
            f'{"#" if siid is None or piid is None else f"{siid}/{piid}"}')

        def on_prop_msg(topic: str, payload: str, ctx: Any) -> bool:
            try:
                msg: dict = json.loads(payload)
            except json.JSONDecodeError:
                self.log_error(
                    f'on_prop_msg, invalid msg, {topic}, {payload}')
                return
            if (
                not isinstance(msg.get('params', None), dict)
                or 'siid' not in msg['params']
                or 'piid' not in msg['params']
                or 'value' not in msg['params']
            ):
                self.log_error(
                    f'on_prop_msg, invalid msg, {topic}, {payload}')
                return
            if handler:
                self.log_debug('on properties_changed, %s', payload)
                handler(msg['params'], ctx)
        return self.__reg_broadcast(
            topic=topic, handler=on_prop_msg, handler_ctx=handler_ctx)

    @final
    def unsub_prop(self, did: str, siid: int = None, piid: int = None) -> bool:
        if not isinstance(did, str):
            raise MIoTMipsError('invalid params')
        topic: str = (
            f'device/{did}/up/properties_changed/'
            f'{"#" if siid is None or piid is None else f"{siid}/{piid}"}')
        return self.__unreg_broadcast(topic=topic)

    @final
    def sub_event(
        self, did: str, handler: Callable[[dict, Any], None],
        siid: int = None, eiid: int = None, handler_ctx: Any = None
    ) -> bool:
        if not isinstance(did, str) or handler is None:
            raise MIoTMipsError('invalid params')
        # Spelling error: event_occured
        topic: str = (
            f'device/{did}/up/event_occured/'
            f'{"#" if siid is None or eiid is None else f"{siid}/{eiid}"}')

        def on_event_msg(topic: str, payload: str, ctx: Any) -> bool:
            try:
                msg: dict = json.loads(payload)
            except json.JSONDecodeError:
                self.log_error(
                    f'on_event_msg, invalid msg, {topic}, {payload}')
                return
            if (
                not isinstance(msg.get('params', None), dict)
                or 'siid' not in msg['params']
                or 'eiid' not in msg['params']
                or 'arguments' not in msg['params']
            ):
                self.log_error(
                    f'on_event_msg, invalid msg, {topic}, {payload}')
                return
            if handler:
                self.log_debug('on on_event_msg, %s', payload)
                msg['params']['from'] = 'cloud'
                handler(msg['params'], ctx)
        return self.__reg_broadcast(
            topic=topic, handler=on_event_msg, handler_ctx=handler_ctx)

    @final
    def unsub_event(self, did: str, siid: int = None, eiid: int = None) -> bool:
        if not isinstance(did, str):
            raise MIoTMipsError('invalid params')
        # Spelling error: event_occured
        topic: str = (
            f'device/{did}/up/event_occured/'
            f'{"#" if siid is None or eiid is None else f"{siid}/{eiid}"}')
        return self.__unreg_broadcast(topic=topic)

    @final
    def sub_device_state(
        self, did: str, handler: Callable[[str, MIoTDeviceState, Any], None],
        handler_ctx: Any = None
    ) -> bool:
        """subscribe online state."""
        if not isinstance(did, str) or handler is None:
            raise MIoTMipsError('invalid params')
        topic: str = f'device/{did}/state/#'

        def on_state_msg(topic: str, payload: str, ctx: Any) -> None:
            msg: dict = json.loads(payload)
            # {"device_id":"xxxx","device_name":"米家智能插座3   ","event":"online",
            # "model": "cuco.plug.v3","timestamp":1709001070828,"uid":xxxx}
            if msg is None or 'device_id' not in msg or 'event' not in msg:
                self.log_error(f'on_state_msg, recv unknown msg, {payload}')
                return
            if msg['device_id'] != did:
                self.log_error(
                    f'on_state_msg, err msg, {did}!={msg["device_id"]}')
                return
            if handler:
                self.log_debug('cloud, device state changed, %s', payload)
                handler(
                    did, MIoTDeviceState.ONLINE if msg['event'] == 'online'
                    else MIoTDeviceState.OFFLINE, ctx)
        return self.__reg_broadcast(
            topic=topic, handler=on_state_msg, handler_ctx=handler_ctx)

    @final
    def unsub_device_state(self, did: str) -> bool:
        if not isinstance(did, str):
            raise MIoTMipsError('invalid params')
        topic: str = f'device/{did}/state/#'
        return self.__unreg_broadcast(topic=topic)

    async def get_dev_list_async(
        self, payload: str = None, timeout_ms: int = 10000
    ) -> dict[str, dict]:
        raise NotImplementedError('please call in http client')

    async def get_prop_async(
        self, did: str, siid: int, piid: int,  timeout_ms: int = 10000
    ) -> Any:
        raise NotImplementedError('please call in http client')

    async def set_prop_async(
        self, did: str, siid: int, piid: int, value: Any,
        timeout_ms: int = 10000
    ) -> bool:
        raise NotImplementedError('please call in http client')

    async def action_async(
        self, did: str, siid: int, aiid: int, in_list: list,
        timeout_ms: int = 10000
    ) -> tuple[bool, list]:
        raise NotImplementedError('please call in http client')

    def __on_mips_cmd_handler(self, mips_cmd: MipsCmd) -> None:
        """
        NOTICE thread safe, this function will be called at the **mips** thread
        """
        if mips_cmd.type_ == MipsCmdType.REG_BROADCAST:
            reg_bc: MipsRegBroadcast = mips_cmd.data
            if not self._msg_matcher.get(topic=reg_bc.topic):
                sub_bc: MipsBroadcast = MipsBroadcast(
                    topic=reg_bc.topic, handler=reg_bc.handler,
                    handler_ctx=reg_bc.handler_ctx)
                self._msg_matcher[reg_bc.topic] = sub_bc
                self._mips_sub_internal(topic=reg_bc.topic)
            else:
                self.log_debug(f'mips cloud re-reg broadcast, {reg_bc.topic}')
        elif mips_cmd.type_ == MipsCmdType.UNREG_BROADCAST:
            unreg_bc: MipsRegBroadcast = mips_cmd.data
            if self._msg_matcher.get(topic=unreg_bc.topic):
                del self._msg_matcher[unreg_bc.topic]
                self._mips_unsub_internal(topic=unreg_bc.topic)

    def __reg_broadcast(
        self, topic: str, handler: Callable[[str, str, Any], None],
        handler_ctx: Any = None
    ) -> bool:
        return self._mips_send_cmd(
            type_=MipsCmdType.REG_BROADCAST,
            data=MipsRegBroadcast(
                topic=topic, handler=handler, handler_ctx=handler_ctx))

    def __unreg_broadcast(self, topic: str) -> bool:
        return self._mips_send_cmd(
            type_=MipsCmdType.UNREG_BROADCAST,
            data=MipsRegBroadcast(topic=topic))

    def __on_mips_connect_handler(self, rc, props) -> None:
        """sub topic."""
        for topic, _ in list(
                self._msg_matcher.iter_all_nodes()):
            self._mips_sub_internal(topic=topic)

    def __on_mips_disconnect_handler(self, rc, props) -> None:
        """unsub topic."""
        pass

    def __on_mips_message_handler(self, topic: str, payload) -> None:
        """
        NOTICE thread safe, this function will be called at the **mips** thread
        """
        # broadcast
        bc_list: list[MipsBroadcast] = list(
            self._msg_matcher.iter_match(topic))
        if not bc_list:
            return
        # self.log_debug(f"on broadcast, {topic}, {payload}")
        for item in bc_list or []:
            if item.handler is None:
                continue
            # NOTICE: call threadsafe
            self.main_loop.call_soon_threadsafe(
                item.handler, topic, payload, item.handler_ctx)


class MipsLocalClient(MipsClient):
    """MIoT Pub/Sub Local Client."""
    # pylint: disable=unused-argument
    # pylint: disable=inconsistent-quotes
    MIPS_RECONNECT_INTERVAL_MIN: int = 6000
    MIPS_RECONNECT_INTERVAL_MAX: int = 60000
    MIPS_SUB_PATCH: int = 1000
    MIPS_SUB_INTERVAL: int = 100
    _did: str
    _group_id: str
    _home_name: str
    _mips_seed_id: int
    _reply_topic: str
    _dev_list_change_topic: str
    _request_map: dict[str, MipsRequest]
    _msg_matcher: MIoTMatcher
    _device_state_sub_map: dict[str, MipsDeviceState]
    _get_prop_queue: dict[str, list]
    _get_prop_timer: asyncio.TimerHandle
    _on_dev_list_changed: Callable[[Any, list[str]], asyncio.Future]

    def __init__(
        self, did: str, host: str, group_id: str,
        ca_file: str, cert_file: str, key_file: str,
        port: int = 8883, home_name: str = '',
        loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._did = did
        self._group_id = group_id
        self._home_name = home_name
        self._mips_seed_id = random.randint(0, self.UINT32_MAX)
        self._reply_topic = f'{did}/reply'
        self._dev_list_change_topic = f'{did}/appMsg/devListChange'
        self._request_map = {}
        self._msg_matcher = MIoTMatcher()
        self._device_state_sub_map = {}
        self._get_prop_queue = {}
        self._get_prop_timer = None
        self._on_dev_list_changed = None

        super().__init__(
            client_id=did, host=host, port=port,
            ca_file=ca_file, cert_file=cert_file, key_file=key_file, loop=loop)
        # MIPS local thread name use group_id
        self._mips_thread.name = self._group_id

        self.on_mips_cmd = self.__on_mips_cmd_handler
        self.on_mips_message = self.__on_mips_message_handler
        self.on_mips_connect = self.__on_mips_connect_handler

    @property
    def group_id(self) -> str:
        return self._group_id

    def deinit(self) -> None:
        self.mips_deinit()
        self._did = None
        self._mips_seed_id = None
        self._reply_topic = None
        self._dev_list_change_topic = None
        self._request_map = None
        self._msg_matcher = None
        self._device_state_sub_map = None
        self._get_prop_queue = None
        self._get_prop_timer = None
        self._on_dev_list_changed = None

        self.on_mips_cmd = None
        self.on_mips_message = None
        self.on_mips_connect = None

    def log_debug(self, msg, *args, **kwargs) -> None:
        if self._logger:
            self._logger.debug(f'{self._home_name}, '+msg, *args, **kwargs)

    def log_info(self, msg, *args, **kwargs) -> None:
        if self._logger:
            self._logger.info(f'{self._home_name}, '+msg, *args, **kwargs)

    def log_error(self, msg, *args, **kwargs) -> None:
        if self._logger:
            self._logger.error(f'{self._home_name}, '+msg, *args, **kwargs)

    @final
    def connect(self) -> None:
        self.mips_connect()

    @final
    async def connect_async(self) -> None:
        await self.mips_connect_async()

    @final
    def disconnect(self) -> None:
        self.mips_disconnect()
        self._request_map = {}
        self._msg_matcher = MIoTMatcher()
        self._device_state_sub_map = {}

    @final
    async def disconnect_async(self) -> None:
        await self.mips_disconnect_async()
        self._request_map = {}
        self._msg_matcher = MIoTMatcher()
        self._device_state_sub_map = {}

    @final
    def sub_prop(
        self, did: str, handler: Callable[[dict, Any], None],
        siid: int = None, piid: int = None, handler_ctx: Any = None
    ) -> bool:
        topic: str = (
            f'appMsg/notify/iot/{did}/property/'
            f'{"#" if siid is None or piid is None else f"{siid}.{piid}"}')

        def on_prop_msg(topic: str, payload: str, ctx: Any):
            msg: dict = json.loads(payload)
            if (
                msg is None
                or 'did' not in msg
                or 'siid' not in msg
                or 'piid' not in msg
                or 'value' not in msg
            ):
                # self.log_error(f'on_prop_msg, recv unknown msg, {payload}')
                return
            if handler:
                self.log_debug('local, on properties_changed, %s', payload)
                handler(msg, ctx)
        return self.__reg_broadcast(
            topic=topic, handler=on_prop_msg, handler_ctx=handler_ctx)

    @final
    def unsub_prop(self, did: str, siid: int = None, piid: int = None) -> bool:
        topic: str = (
            f'appMsg/notify/iot/{did}/property/'
            f'{"#" if siid is None or piid is None else f"{siid}.{piid}"}')
        return self.__unreg_broadcast(topic=topic)

    @final
    def sub_event(
        self, did: str, handler: Callable[[dict, Any], None],
        siid: int = None, eiid: int = None, handler_ctx: Any = None
    ) -> bool:
        topic: str = (
            f'appMsg/notify/iot/{did}/event/'
            f'{"#" if siid is None or eiid is None else f"{siid}.{eiid}"}')

        def on_event_msg(topic: str, payload: str, ctx: Any):
            msg: dict = json.loads(payload)
            if (
                msg is None
                or 'did' not in msg
                or 'siid' not in msg
                or 'eiid' not in msg
                or 'arguments' not in msg
            ):
                # self.log_error(f'on_event_msg, recv unknown msg, {payload}')
                return
            if handler:
                self.log_debug('local, on event_occurred, %s', payload)
                handler(msg, ctx)
        return self.__reg_broadcast(
            topic=topic, handler=on_event_msg, handler_ctx=handler_ctx)

    @final
    def unsub_event(self, did: str, siid: int = None, eiid: int = None) -> bool:
        topic: str = (
            f'appMsg/notify/iot/{did}/event/'
            f'{"#" if siid is None or eiid is None else f"{siid}.{eiid}"}')
        return self.__unreg_broadcast(topic=topic)

    @final
    async def get_prop_safe_async(
        self, did: str, siid: int, piid: int, timeout_ms: int = 10000
    ) -> Any:
        self._get_prop_queue.setdefault(did, [])
        fut: asyncio.Future = self.main_loop.create_future()
        self._get_prop_queue[did].append({
            'param': json.dumps({
                'did': did,
                'siid': siid,
                'piid': piid
            }),
            'fut': fut,
            'timeout_ms': timeout_ms
        })
        if self._get_prop_timer is None:
            self._get_prop_timer = self.main_loop.create_task(
                self.__get_prop_timer_handle())
        return await fut

    @final
    async def get_prop_async(
        self, did: str, siid: int, piid: int, timeout_ms: int = 10000
    ) -> Any:
        result_obj = await self.__request_async(
            topic='proxy/get',
            payload=json.dumps({
                'did': did,
                'siid': siid,
                'piid': piid
            }),
            timeout_ms=timeout_ms)
        if not isinstance(result_obj, dict) or 'value' not in result_obj:
            return None
        return result_obj['value']

    @final
    async def set_prop_async(
        self, did: str, siid: int, piid: int, value: Any,
        timeout_ms: int = 10000
    ) -> dict:
        payload_obj: dict = {
            'did': did,
            'rpc': {
                'id': self.__gen_mips_id,
                'method': 'set_properties',
                'params': [{
                    'did': did,
                    'siid': siid,
                    'piid': piid,
                    'value': value
                }]
            }
        }
        result_obj = await self.__request_async(
            topic='proxy/rpcReq',
            payload=json.dumps(payload_obj),
            timeout_ms=timeout_ms)
        if result_obj:
            if (
                'result' in result_obj
                and len(result_obj['result']) == 1
                and 'did' in result_obj['result'][0]
                and result_obj['result'][0]['did'] == did
                and 'code' in result_obj['result'][0]
            ):
                return result_obj['result'][0]
            if 'error' in result_obj:
                return result_obj['error']
        return {
            'code': MIoTErrorCode.CODE_INTERNAL_ERROR.value,
            'message': 'Invalid result'}

    @final
    async def action_async(
        self, did: str, siid: int, aiid: int, in_list: list,
        timeout_ms: int = 10000
    ) -> dict:
        payload_obj: dict = {
            'did': did,
            'rpc': {
                'id': self.__gen_mips_id,
                'method': 'action',
                'params': {
                    'did': did,
                    'siid': siid,
                    'aiid': aiid,
                    'in': in_list
                }
            }
        }
        result_obj = await self.__request_async(
            topic='proxy/rpcReq', payload=json.dumps(payload_obj),
            timeout_ms=timeout_ms)
        if result_obj:
            if 'result' in result_obj and 'code' in result_obj['result']:
                return result_obj['result']
            if 'error' in result_obj:
                return result_obj['error']
        return {
            'code': MIoTErrorCode.CODE_INTERNAL_ERROR.value,
            'message': 'Invalid result'}

    @final
    async def get_dev_list_async(
        self, payload: str = None, timeout_ms: int = 10000
    ) -> dict[str, dict]:
        result_obj = await self.__request_async(
            topic='proxy/getDevList', payload=payload or '{}',
            timeout_ms=timeout_ms)
        if not result_obj or 'devList' not in result_obj:
            return None
        device_list = {}
        for did, info in result_obj['devList'].items():
            name: str = info.get('name', None)
            urn: str = info.get('urn', None)
            model: str = info.get('model', None)
            if name is None or urn is None or model is None:
                self.log_error(f'invalid device info, {did}, {info}')
                continue
            device_list[did] = {
                'did': did,
                'name': name,
                'urn': urn,
                'model': model,
                'online': info.get('online', False),
                'icon': info.get('icon', None),
                'fw_version': None,
                'home_id': '',
                'home_name': '',
                'room_id': info.get('roomId', ''),
                'room_name': info.get('roomName', ''),
                'specv2_access': info.get('specV2Access', False),
                'push_available': info.get('pushAvailable', False),
                'manufacturer': model.split('.')[0],
            }
        return device_list

    @final
    async def get_action_group_list_async(
        self, timeout_ms: int = 10000
    ) -> list[str]:
        result_obj = await self.__request_async(
            topic='proxy/getMijiaActionGroupList',
            payload='{}',
            timeout_ms=timeout_ms)
        if not result_obj or 'result' not in result_obj:
            return None
        return result_obj['result']

    @final
    async def exec_action_group_list_async(
        self, ag_id: str, timeout_ms: int = 10000
    ) -> dict:
        result_obj = await self.__request_async(
            topic='proxy/execMijiaActionGroup',
            payload=f'{{"id":"{ag_id}"}}',
            timeout_ms=timeout_ms)
        if result_obj:
            if 'result' in result_obj:
                return result_obj['result']
            if 'error' in result_obj:
                return result_obj['error']
        return {
            'code': MIoTErrorCode.CODE_MIPS_INVALID_RESULT.value,
            'message': 'invalid result'}

    @final
    @property
    def on_dev_list_changed(self) -> Callable[[Any, list[str]], asyncio.Future]:
        return self._on_dev_list_changed

    @final
    @on_dev_list_changed.setter
    def on_dev_list_changed(
        self, func: Callable[[Any, list[str]], asyncio.Future]
    ) -> None:
        """run in main loop."""
        self._on_dev_list_changed = func

    @final
    def __on_mips_cmd_handler(self, mips_cmd: MipsCmd) -> None:
        if mips_cmd.type_ == MipsCmdType.CALL_API:
            req_data: MipsRequestData = mips_cmd.data
            req = MipsRequest()
            req.mid = self.__gen_mips_id
            req.on_reply = req_data.on_reply
            req.on_reply_ctx = req_data.on_reply_ctx
            pub_topic: str = f'master/{req_data.topic}'
            result = self.__mips_publish(
                topic=pub_topic, payload=req_data.payload, mid=req.mid,
                ret_topic=self._reply_topic)
            self.log_debug(
                f'mips local call api, {result}, {req.mid}, {pub_topic}, '
                f'{req_data.payload}')

            def on_request_timeout(req: MipsRequest):
                self.log_error(
                    f'on mips request timeout, {req.mid}, {pub_topic}'
                    f', {req_data.payload}')
                self._request_map.pop(str(req.mid), None)
                req.on_reply(
                    '{"error":{"code":-10006, "message":"timeout"}}',
                    req.on_reply_ctx)
            req.timer = self.mev_set_timeout(
                req_data.timeout_ms, on_request_timeout, req)
            self._request_map[str(req.mid)] = req
        elif mips_cmd.type_ == MipsCmdType.REG_BROADCAST:
            reg_bc: MipsRegBroadcast = mips_cmd.data
            sub_topic: str = f'{self._did}/{reg_bc.topic}'
            if not self._msg_matcher.get(sub_topic):
                sub_bc: MipsBroadcast = MipsBroadcast(
                    topic=sub_topic, handler=reg_bc.handler,
                    handler_ctx=reg_bc.handler_ctx)
                self._msg_matcher[sub_topic] = sub_bc
                self._mips_sub_internal(topic=f'master/{reg_bc.topic}')
            else:
                self.log_debug(f'mips re-reg broadcast, {sub_topic}')
        elif mips_cmd.type_ == MipsCmdType.UNREG_BROADCAST:
            unreg_bc: MipsRegBroadcast = mips_cmd.data
            # Central hub gateway needs to add prefix
            unsub_topic: str = f'{self._did}/{unreg_bc.topic}'
            if self._msg_matcher.get(unsub_topic):
                del self._msg_matcher[unsub_topic]
                self._mips_unsub_internal(
                    topic=re.sub(f'^{self._did}', 'master', unsub_topic))
        elif mips_cmd.type_ == MipsCmdType.REG_DEVICE_STATE:
            reg_dev_state: MipsRegDeviceState = mips_cmd.data
            self._device_state_sub_map[reg_dev_state.did] = reg_dev_state
            self.log_debug(
                f'mips local reg device state, {reg_dev_state.did}')
        elif mips_cmd.type_ == MipsCmdType.UNREG_DEVICE_STATE:
            unreg_dev_state: MipsRegDeviceState = mips_cmd.data
            del self._device_state_sub_map[unreg_dev_state.did]
            self.log_debug(
                f'mips local unreg device state, {unreg_dev_state.did}')
        else:
            self.log_error(
                f'mips local recv unknown cmd, {mips_cmd.type_}, '
                f'{mips_cmd.data}')

    def __on_mips_connect_handler(self, rc, props) -> None:
        self.log_debug('__on_mips_connect_handler')
        # Sub did/#, include reply topic
        self._mips_sub_internal(f'{self._did}/#')
        # Sub device list change
        self._mips_sub_internal('master/appMsg/devListChange')
        # Do not need to subscribe api topics, for they are covered by did/#
        # Sub api topic.
        # Sub broadcast topic
        for topic, _ in list(self._msg_matcher.iter_all_nodes()):
            self._mips_sub_internal(
                topic=re.sub(f'^{self._did}', 'master', topic))

    @final
    def __on_mips_message_handler(self, topic: str, payload: bytes) -> None:
        mips_msg: MipsMessage = MipsMessage.unpack(payload)
        # self.log_debug(
        #     f"mips local client, on_message, {topic} -> {mips_msg}")
        # Reply
        if topic == self._reply_topic:
            self.log_debug(f'on request reply, {mips_msg}')
            req: MipsRequest = self._request_map.pop(str(mips_msg.mid), None)
            if req:
                # Cancel timer
                self.mev_clear_timeout(req.timer)
                if req.on_reply:
                    self.main_loop.call_soon_threadsafe(
                        req.on_reply, mips_msg.payload or '{}',
                        req.on_reply_ctx)
            return
        # Broadcast
        bc_list: list[MipsBroadcast] = list(self._msg_matcher.iter_match(
            topic=topic))
        if bc_list:
            self.log_debug(f'on broadcast, {topic}, {mips_msg}')
            for item in bc_list or []:
                if item.handler is None:
                    continue
                self.main_loop.call_soon_threadsafe(
                    item.handler, topic[topic.find('/')+1:],
                    mips_msg.payload or '{}', item.handler_ctx)
            return
        # Device list change
        if topic == self._dev_list_change_topic:
            payload_obj: dict = json.loads(mips_msg.payload)
            dev_list = payload_obj.get('devList', None)
            if not isinstance(dev_list, list) or not dev_list:
                _LOGGER.error(
                    'unknown devListChange msg, %s', mips_msg.payload)
                return
            if self._on_dev_list_changed:
                self.main_loop.call_soon_threadsafe(
                    self.main_loop.create_task,
                    self._on_dev_list_changed(self, payload_obj['devList']))
            return

        self.log_debug(
            f'mips local client, recv unknown msg, {topic} -> {mips_msg}')

    @property
    def __gen_mips_id(self) -> int:
        mips_id: int = self._mips_seed_id
        self._mips_seed_id = int((self._mips_seed_id+1) % self.UINT32_MAX)
        return mips_id

    def __mips_publish(
            self, topic: str, payload: str | bytes, mid: int = None,
            ret_topic: str = None, wait_for_publish: bool = False,
            timeout_ms: int = 10000
    ) -> bool:
        mips_msg: bytes = MipsMessage.pack(
            mid=mid or self.__gen_mips_id, payload=payload,
            msg_from='local', ret_topic=ret_topic)
        return self._mips_publish_internal(
            topic=topic.strip(), payload=mips_msg,
            wait_for_publish=wait_for_publish, timeout_ms=timeout_ms)

    def __request(
            self, topic: str, payload: str,
            on_reply: Callable[[str, Any], None],
            on_reply_ctx: Any = None, timeout_ms: int = 10000
    ) -> bool:
        if topic is None or payload is None or on_reply is None:
            raise MIoTMipsError('invalid params')
        req_data: MipsRequestData = MipsRequestData()
        req_data.topic = topic
        req_data.payload = payload
        req_data.on_reply = on_reply
        req_data.on_reply_ctx = on_reply_ctx
        req_data.timeout_ms = timeout_ms
        return self._mips_send_cmd(type_=MipsCmdType.CALL_API, data=req_data)

    def __reg_broadcast(
        self, topic: str, handler: Callable[[str, str, Any], None],
        handler_ctx: Any
    ) -> bool:
        return self._mips_send_cmd(
            type_=MipsCmdType.REG_BROADCAST,
            data=MipsRegBroadcast(
                topic=topic, handler=handler, handler_ctx=handler_ctx))

    def __unreg_broadcast(self, topic) -> bool:
        return self._mips_send_cmd(
            type_=MipsCmdType.UNREG_BROADCAST,
            data=MipsRegBroadcast(topic=topic))

    @final
    async def __request_async(
        self, topic: str, payload: str, timeout_ms: int = 10000
    ) -> dict:
        fut_handler: asyncio.Future = self.main_loop.create_future()

        def on_msg_reply(payload: str, ctx: Any):
            fut: asyncio.Future = ctx
            if fut:
                self.main_loop.call_soon_threadsafe(fut.set_result, payload)
        if not self.__request(
                topic=topic,
                payload=payload,
                on_reply=on_msg_reply,
                on_reply_ctx=fut_handler,
                timeout_ms=timeout_ms):
            # Request error
            fut_handler.set_result('internal request error')

        result = await fut_handler
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {
                'code': MIoTErrorCode.CODE_MIPS_INVALID_RESULT.value,
                'message': f'Error: {result}'}

    async def __get_prop_timer_handle(self) -> None:
        for did in list(self._get_prop_queue.keys()):
            item = self._get_prop_queue[did].pop()
            _LOGGER.debug('get prop, %s, %s', did, item)
            result_obj = await self.__request_async(
                topic='proxy/get',
                payload=item['param'],
                timeout_ms=item['timeout_ms'])
            if result_obj is None or 'value' not in result_obj:
                item['fut'].set_result(None)
            else:
                item['fut'].set_result(result_obj['value'])

            if not self._get_prop_queue[did]:
                self._get_prop_queue.pop(did, None)

        if self._get_prop_queue:
            self._get_prop_timer = self.main_loop.call_later(
                0.1, lambda: self.main_loop.create_task(
                    self.__get_prop_timer_handle()))
        else:
            self._get_prop_timer = None
