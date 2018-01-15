from typing import Optional, List, Coroutine, Union, Dict, Callable, AnyStr, IO, DefaultDict
import abc
import itertools
import logging
import time
from collections import defaultdict

import aiohttp
from aiohttp.client_reqrep import ClientResponse
from aiohttp.client import DEFAULT_TIMEOUT
from aiohttp import ClientSession
import async_timeout
from yarl import URL
from tenacity import retry
from tenacity.retry import retry_if_result, retry_if_exception_type
from tenacity.wait import wait_fixed
from tenacity.stop import stop_after_attempt

from .pipelines import Pipeline
from .things import Request, Response, Item, Things
from . import queen


__all__ = ['Ant']


class Ant(abc.ABC):
    response_pipelines = []  # type: List[Pipeline]
    request_pipelines = []  # type: List[Pipeline]
    item_pipelines = []  # type: List[Pipeline]
    request_timeout = DEFAULT_TIMEOUT
    request_retries = 3
    request_retry_delay = 5
    request_proxy = None  # type: Optional[str]
    request_max_redirects = 10
    request_allow_redirects = True

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.sessions = {}  # type: Dict[str, ClientSession]
        # report var
        self._reports = defaultdict(lambda: [0, 0])  # type: DefaultDict[str, List[int, int]]
        self._drop_reports = defaultdict(lambda: [0, 0])  # type: DefaultDict[str, List[int, int]]
        self._start_time = time.time()
        self._last_time = self._start_time
        self._report_slot = 60  # report once after one minute by default

    async def request(self, url: Union[str, URL], method='GET', params: Optional[dict]=None,
                      headers: Optional[dict]=None, cookies: Optional[dict]=None,
                      data: Optional[Union[AnyStr, Dict, IO]]=None,
                      ) -> Response:
        req = Request(url, method=method, params=params, headers=headers, cookies=cookies, data=data)
        req = await self._handle_thing_with_pipelines(req, self.request_pipelines, timeout=self.request_timeout)
        self.report(req)

        request_function = queen.timeout_wrapper(self._request, timeout=self.request_timeout)
        retries = self.request_retries
        if retries > 0:
            res = await self.make_retry_decorator(retries, self.request_retry_delay)(request_function)(req)
        else:
            res = await request_function(req)

        res = await self._handle_thing_with_pipelines(res, self.response_pipelines, timeout=self.request_timeout)
        self.report(res)
        return res

    async def collect(self, item: Item) -> None:
        self.logger.debug('Collect item: ' + str(item))
        await self._handle_thing_with_pipelines(item, self.item_pipelines)
        self.report(item)

    async def open(self) -> None:
        self.logger.info('Opening')
        for pipeline in itertools.chain(self.item_pipelines, self.response_pipelines, self.request_pipelines):
            try:
                obj = pipeline.on_spider_open()
                if isinstance(obj, Coroutine):
                    await obj
            except Exception as e:
                self.logger.exception('Open pipelines with ' + e.__class__.__name__)

    async def close(self) -> None:
        for pipeline in itertools.chain(self.item_pipelines, self.response_pipelines, self.request_pipelines):
            try:
                obj = pipeline.on_spider_close()
                if isinstance(obj, Coroutine):
                    await obj
            except Exception as e:
                self.logger.exception('Close pipelines with ' + e.__class__.__name__)
        # close cached sessions
        for session in self.sessions.values():
            await session.close()
        self.logger.info('Closed')

    @abc.abstractmethod
    async def run(self) -> None:
        """App custom entrance"""

    async def main(self) -> None:
        await self.open()
        try:
            await self.run()
        except Exception as e:
            self.logger.exception('Run ant run`s coroutine with ' + e.__class__.__name__)
        # wait scheduled coroutines before wait "close" method
        await queen.wait_scheduled_coroutines()
        await self.close()
        await queen.wait_scheduled_coroutines()
        # total report
        for name, counts in self._reports.items():
            self.logger.info('Get {:d} {:s} in total'.format(counts[1], name))
        for name, counts in self._drop_reports.items():
            self.logger.info('Drop {:d} {:s} in total'.format(counts[1], name))
        self.logger.info('Run {:s} in {:f} seconds'.format(self.__class__.__name__, time.time() - self._start_time))

    @staticmethod
    def make_retry_decorator(retries: int, delay: float) -> Callable[[Callable], Callable]:
        return retry(wait=wait_fixed(delay),
                     retry=(retry_if_result(lambda res: res.status >= 500) | retry_if_exception_type()),
                     stop=stop_after_attempt(retries + 1))

    async def _handle_thing_with_pipelines(self, thing: Things, pipelines: List[Pipeline],
                                           timeout=DEFAULT_TIMEOUT) -> Things:
        """Process thing one by one, break the process chain when get "None" or exception
        :raise ThingDropped"""
        self.logger.debug('Process thing: ' + str(thing))
        raw_thing = thing
        for pipeline in pipelines:
            thing = pipeline.process(thing)
            if isinstance(thing, Coroutine):
                with async_timeout.timeout(timeout):
                    thing = await thing
            if isinstance(thing, Exception):
                self.report(raw_thing, dropped=True)
                raise thing
        return thing

    async def _request(self, req: Request) -> Response:
        kwargs = {k: getattr(req, k) for k in req.__slots__}
        cookies = kwargs.pop('cookies')
        kwargs['proxy'] = self.request_proxy
        kwargs['max_redirects'] = self.request_max_redirects
        kwargs['allow_redirects'] = self.request_allow_redirects

        # proxy auth not work when one session with many requests, add auth header to fix it
        proxy = None if kwargs['proxy'] is None else URL(kwargs['proxy'])
        if proxy is not None and proxy.user is not None:
            if kwargs['headers'] is None:
                kwargs['headers'] = dict()
            kwargs['headers'][aiohttp.hdrs.PROXY_AUTHORIZATION] = aiohttp.BasicAuth.from_url(proxy).encode()

        host = req.url.host
        if host not in self.sessions:
            session = aiohttp.ClientSession(cookies=cookies)
            self.sessions[host] = session
        else:
            session = self.sessions[host]
            if cookies is not None:
                session.cookie_jar.update_cookies(cookies)

        async with session.request(**kwargs) as aio_response:
            await aio_response.read()
        return self._convert_response(aio_response, req)

    @staticmethod
    def _convert_response(aio_response: ClientResponse, request: Request) -> Response:
        return Response(request, aio_response.status, aio_response._content,
                        headers=aio_response.headers, cookies=aio_response.cookies,
                        encoding=aio_response._get_encoding())

    def report(self, thing: Things, dropped: bool=False) -> None:
        now_time = time.time()
        if now_time - self._last_time > self._report_slot:
            self._last_time = now_time
            for name, counts in self._reports.items():
                count = counts[1] - counts[0]
                counts[0] = counts[1]
                self.logger.info(
                    'Get {:d} {:s} in total with {:d}/{:d}s rate'.format(
                        counts[1], name, count, self._report_slot))
            for name, counts in self._drop_reports.items():
                count = counts[1] - counts[0]
                counts[0] = counts[1]
                self.logger.info(
                    'Drop {:d} {:s} in total with {:d}/{:d} rate'.format(
                        counts[1], name, count, self._report_slot))
        report_type = thing.__class__.__name__
        if dropped:
            reports = self._drop_reports
        else:
            reports = self._reports
        counts = reports[report_type]
        counts[1] += 1
