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

MIoT event loop.
"""
import selectors
import heapq
import time
import traceback
from typing import Any, Callable, TypeVar
import logging
import threading

# pylint: disable=relative-beyond-top-level
from .miot_error import MIoTEvError

_LOGGER = logging.getLogger(__name__)

TimeoutHandle = TypeVar('TimeoutHandle')


class MIoTFdHandler:
    """File descriptor handler."""
    fd: int
    read_handler: Callable[[Any], None]
    read_handler_ctx: Any
    write_handler: Callable[[Any], None]
    write_handler_ctx: Any

    def __init__(
            self, fd: int,
            read_handler: Callable[[Any], None] = None,
            read_handler_ctx: Any = None,
            write_handler: Callable[[Any], None] = None,
            write_handler_ctx: Any = None
    ) -> None:
        self.fd = fd
        self.read_handler = read_handler
        self.read_handler_ctx = read_handler_ctx
        self.write_handler = write_handler
        self.write_handler_ctx = write_handler_ctx


class MIoTTimeout:
    """Timeout handler."""
    key: TimeoutHandle
    target: int
    handler: Callable[[Any], None]
    handler_ctx: Any

    def __init__(
            self, key: str = None, target: int = None,
            handler: Callable[[Any], None] = None,
            handler_ctx: Any = None
    ) -> None:
        self.key = key
        self.target = target
        self.handler = handler
        self.handler_ctx = handler_ctx

    def __lt__(self, other):
        return self.target < other.target


class MIoTEventLoop:
    """MIoT event loop."""
    _poll_fd: selectors.DefaultSelector

    _fd_handlers: dict[str, MIoTFdHandler]

    _timer_heap: list[MIoTTimeout]
    _timer_handlers: dict[str, MIoTTimeout]
    _timer_handle_seed: int

    # Label if the current fd handler is freed inside a read handler to
    # avoid invalid reading.
    _fd_handler_freed_in_read_handler: bool

    def __init__(self) -> None:
        self._poll_fd = selectors.DefaultSelector()
        self._timer_heap = []
        self._timer_handlers = {}
        self._timer_handle_seed = 1
        self._fd_handlers = {}
        self._fd_handler_freed_in_read_handler = False

    def loop_forever(self) -> None:
        """Run an event loop in current thread."""
        next_timeout: int
        while True:
            next_timeout = 0
            # Handle timer
            now_ms: int = self.__get_monotonic_ms
            while len(self._timer_heap) > 0:
                timer: MIoTTimeout = self._timer_heap[0]
                if timer is None:
                    break
                if timer.target <= now_ms:
                    heapq.heappop(self._timer_heap)
                    del self._timer_handlers[timer.key]
                    if timer.handler:
                        timer.handler(timer.handler_ctx)
                else:
                    next_timeout = timer.target-now_ms
                    break
            # Are there any files to listen to
            if next_timeout == 0 and self._fd_handlers:
                next_timeout = None  # None == infinite
            # Wait for timers & fds
            if next_timeout == 0:
                # Neither timer nor fds exist, exit loop
                break
            # Handle fd event
            events = self._poll_fd.select(
                timeout=next_timeout/1000.0 if next_timeout else next_timeout)
            for key, mask in events:
                fd_handler: MIoTFdHandler = key.data
                if fd_handler is None:
                    continue
                self._fd_handler_freed_in_read_handler = False
                fd_key = str(id(fd_handler.fd))
                if fd_key not in self._fd_handlers:
                    continue
                if (
                    mask & selectors.EVENT_READ > 0
                    and fd_handler.read_handler
                ):
                    fd_handler.read_handler(fd_handler.read_handler_ctx)
                if (
                    mask & selectors.EVENT_WRITE > 0
                    and self._fd_handler_freed_in_read_handler is False
                    and fd_handler.write_handler
                ):
                    fd_handler.write_handler(fd_handler.write_handler_ctx)

    def loop_stop(self) -> None:
        """Stop the event loop."""
        if self._poll_fd:
            self._poll_fd.close()
            self._poll_fd = None
            self._fd_handlers = {}
            self._timer_heap = []
            self._timer_handlers = {}

    def set_timeout(
        self, timeout_ms: int, handler: Callable[[Any], None],
        handler_ctx: Any = None
    ) -> TimeoutHandle:
        """Set a timer."""
        if timeout_ms is None or handler is None:
            raise MIoTEvError('invalid params')
        new_timeout: MIoTTimeout = MIoTTimeout()
        new_timeout.key = self.__get_next_timeout_handle
        new_timeout.target = self.__get_monotonic_ms + timeout_ms
        new_timeout.handler = handler
        new_timeout.handler_ctx = handler_ctx
        heapq.heappush(self._timer_heap, new_timeout)
        self._timer_handlers[new_timeout.key] = new_timeout
        return new_timeout.key

    def clear_timeout(self, timer_key: TimeoutHandle) -> None:
        """Stop and remove the timer."""
        if timer_key is None:
            return
        timer: MIoTTimeout = self._timer_handlers.pop(timer_key, None)
        if timer:
            self._timer_heap = list(self._timer_heap)
            self._timer_heap.remove(timer)
            heapq.heapify(self._timer_heap)

    def set_read_handler(
        self, fd: int, handler: Callable[[Any], None], handler_ctx: Any = None
    ) -> bool:
        """Set a read handler for a file descriptor.

        Returns:
            bool: True, success. False, failed.
        """
        self.__set_handler(
            fd, is_read=True, handler=handler, handler_ctx=handler_ctx)

    def set_write_handler(
        self, fd: int, handler: Callable[[Any], None], handler_ctx: Any = None
    ) -> bool:
        """Set a write handler for a file descriptor.

        Returns:
            bool: True, success. False, failed.
        """
        self.__set_handler(
            fd, is_read=False, handler=handler, handler_ctx=handler_ctx)

    def __set_handler(
        self, fd, is_read: bool, handler: Callable[[Any], None],
        handler_ctx: Any = None
    ) -> bool:
        """Set a handler."""
        if fd is None:
            raise MIoTEvError('invalid params')

        if not self._poll_fd:
            raise MIoTEvError('event loop not started')

        fd_key: str = str(id(fd))
        fd_handler = self._fd_handlers.get(fd_key, None)

        if fd_handler is None:
            fd_handler = MIoTFdHandler(fd=fd)
            fd_handler.fd = fd
            self._fd_handlers[fd_key] = fd_handler

        read_handler_existed = fd_handler.read_handler is not None
        write_handler_existed = fd_handler.write_handler is not None
        if is_read is True:
            fd_handler.read_handler = handler
            fd_handler.read_handler_ctx = handler_ctx
        else:
            fd_handler.write_handler = handler
            fd_handler.write_handler_ctx = handler_ctx

        if fd_handler.read_handler is None and fd_handler.write_handler is None:
            # Remove from epoll and map
            try:
                self._poll_fd.unregister(fd)
            except (KeyError, ValueError, OSError) as e:
                del e
            self._fd_handlers.pop(fd_key, None)
            # May be inside a read handler, if not, this has no effect
            self._fd_handler_freed_in_read_handler = True
        elif read_handler_existed is False and write_handler_existed is False:
            # Add to epoll
            events = 0x0
            if fd_handler.read_handler:
                events |= selectors.EVENT_READ
            if fd_handler.write_handler:
                events |= selectors.EVENT_WRITE
            try:
                self._poll_fd.register(fd, events=events, data=fd_handler)
            except (KeyError, ValueError, OSError) as e:
                _LOGGER.error(
                    '%s, register fd, error, %s, %s, %s, %s, %s',
                    threading.current_thread().name,
                    'read' if is_read else 'write',
                    fd_key, handler, e, traceback.format_exc())
                self._fd_handlers.pop(fd_key, None)
                return False
        elif (
            read_handler_existed != (fd_handler.read_handler is not None)
            or write_handler_existed != (fd_handler.write_handler is not None)
        ):
            # Modify epoll
            events = 0x0
            if fd_handler.read_handler:
                events |= selectors.EVENT_READ
            if fd_handler.write_handler:
                events |= selectors.EVENT_WRITE
            try:
                self._poll_fd.modify(fd, events=events, data=fd_handler)
            except (KeyError, ValueError, OSError) as e:
                _LOGGER.error(
                    '%s, modify fd, error, %s, %s, %s, %s, %s',
                    threading.current_thread().name,
                    'read' if is_read else 'write',
                    fd_key, handler, e, traceback.format_exc())
                self._fd_handlers.pop(fd_key, None)
                return False

        return True

    @property
    def __get_next_timeout_handle(self) -> str:
        # Get next timeout handle, that is not larger than the maximum
        # value of UINT64 type.
        self._timer_handle_seed += 1
        # uint64 max
        self._timer_handle_seed %= 0xFFFFFFFFFFFFFFFF
        return str(self._timer_handle_seed)

    @property
    def __get_monotonic_ms(self) -> int:
        """Get monotonic ms timestamp."""
        return int(time.monotonic()*1000)
