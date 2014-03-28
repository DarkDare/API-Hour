import collections
import email
import email.message
import functools
import inspect
import json
import re
import time

import asyncio
import aiohttp, aiohttp.server

from types import MethodType
from datetime import datetime, timedelta
from urllib.parse import urlsplit, parse_qsl

from .multidict import MultiDict


Entry = collections.namedtuple('Entry', 'regex method handler use_request')


class Request:

    def __init__(self, host, message, headers, req_body):
        self.version = message.version
        self.method = message.method.upper()
        self.host = headers.get('HOST', host)
        self.host_url = 'http://' + self.host
        self.path_qs = message.path
        res = urlsplit(self.path_qs)
        self.path = res.path
        self.path_url = self.host_url + self.path
        self.url = self.host_url + self.path_qs
        self.query_string = res.query
        self.args = MultiDict(parse_qsl(self.query_string))
        if req_body:
            self.json_body = json.loads(req_body.decode('utf-8'))
        else:
            self.json_body = None
        self.request_headers = headers
        self.response_headers = email.message.Message()

    @property
    def cookies(self):
        """Return all cookies.

        A dictionary of Cookie.Morsel objects.
        """
        pass


class Session(collections.MutableMapping):

    def __init__(self):
        self._changed = False
        self._mapping = {}

    @property
    def new(self):
        return False

    def __len__(self):
        return len(self._mapping)

    def __contains__(self, key):
        return key in self._mapping

    def __getitem__(self, key):
        return self._mapping[key]

    def __setitem__(self, key, value):
        self._mapping[key] = value
        self._changed = True

    def __delitem__(self, key):
        del self._mapping[key]
        self._changed = True

    @property
    def created(self):
        return datetime.datetime.now()

    def changed(self):
        self._changed = True

    def invalidate(self):
        #TODO: invalidate cookies
        self._changed = True
        self._mapping = {}

    def get_csrf_token(self):
        return "current_csrf_tocken or create new if not exists"

    def new_csrf_token(self):
        return new_csrf_token


class RESTServer(aiohttp.server.ServerHttpProtocol):

    DYN = re.compile(r'^\{[_a-zA-Z][_a-zA-Z0-9]*\}$')
    PLAIN = re.compile(r'^[^{}/]+$')

    METHODS = {'POST', 'GET', 'PUT', 'DELETE', 'PATCH', 'HEAD'}

    def __init__(self, *, hostname, **kwargs):
        super().__init__(**kwargs)
        self.hostname = hostname
        self._urls = []

    @asyncio.coroutine
    def handle_request(self, message, payload):
        now = time.time()
        #self.log.debug("Start handle request %r at %d", message, now)

        try:
            headers = email.message.Message()
            for hdr, val in message.headers:
                headers.add_header(hdr, val)

            if payload is not None:
                req_body = bytearray()
                try:
                    while True:
                        chunk = yield from payload.read()
                        req_body.extend(chunk)
                except aiohttp.EofStream:
                    pass
            else:
                post_body = None

            request = Request(self.hostname, message, headers, req_body)

            response = aiohttp.Response(self.transport, 200)
            body = yield from self.dispatch(request)
            bbody = body.encode('utf-8')

            response.add_header('Host', self.hostname)
            response.add_header('Content-Type', 'application/json')

            # content encoding
            ## accept_encoding = headers.get('accept-encoding', '').lower()
            ## if 'deflate' in accept_encoding:
            ##     response.add_header('Transfer-Encoding', 'chunked')
            ##     response.add_chunking_filter(1025)
            ##     response.add_header('Content-Encoding', 'deflate')
            ##     response.add_compression_filter('deflate')
            ## elif 'gzip' in accept_encoding:
            ##     response.add_header('Transfer-Encoding', 'chunked')
            ##     response.add_chunking_filter(1025)
            ##     response.add_header('Content-Encoding', 'gzip')
            ##     response.add_compression_filter('gzip')
            ## else:
            response.add_header('Content-Length', str(len(bbody)))

            response.send_headers()
            response.write(bbody)
            response.write_eof()
            if response.keep_alive():
                self.keep_alive(True)

            #self.log.debug("Fihish handle request %r at %d -> %s",
            #               message, time.time(), body)
            self.log_access(message, None, response, time.time() - now)
        except Exception as exc:
            #self.log.exception("Cannot handle request %r", message)
            raise

    def add_url(self, method, path, handler, use_request=False):
        assert callable(handler), handler
        if isinstance(handler, MethodType):
            holder = handler.__func__
        else:
            holder = handler
        sig = holder.__signature__ = inspect.signature(handler)

        if use_request:
            if use_request == True:
                use_request = 'request'
            try:
                p = sig.parameters[use_request]
            except KeyError:
                raise TypeError('handler {!r} has no argument {}'
                                .format(handler, use_request))
            assert p.annotation is p.empty, ("handler's arg {} "
                                             "for request name "
                                             "should not have "
                                             "annotation").format(use_request)
        else:
            use_request = None
        assert path.startswith('/')
        assert callable(handler), handler
        method = method.upper()
        assert method in self.METHODS, method
        regexp = []
        for part in path.split('/'):
            if not part:
                continue
            if self.DYN.match(part):
                regexp.append('(?P<'+part[1:-1]+'>.+)')
            elif self.PLAIN.match(part):
                regexp.append(part)
            else:
                raise ValueError("Invalid path '{}'['{}']".format(path, part))
        pattern = '/' + '/'.join(regexp)
        if path.endswith('/') and pattern != '/':
            pattern += '/'
        try:
            compiled = re.compile('^' + pattern + '$')
        except re.error:
            raise ValueError("Invalid path '{}'".format(path))
        self._urls.append(Entry(compiled, method, handler, use_request))

    @asyncio.coroutine
    def dispatch(self, request):
        path = request.path
        method = request.method
        allowed_methods = set()
        for entry in self._urls:
            match = entry.regex.match(path)
            if match is None:
                continue
            if entry.method != method:
                allowed_methods.add(entry.method)
            else:
                break
        else:
            if allowed_methods:
                allow = ', '.join(sorted(allowed_methods))
                # add log
                raise aiohttp.HttpErrorException(405,
                                                 headers=(('Allow', allow),))
            else:
                # add log
                raise aiohttp.HttpErrorException(404, "Not Found")

        handler = entry.handler
        sig = inspect.signature(handler)
        kwargs = match.groupdict()
        if entry.use_request:
            assert entry.use_request not in kwargs, (entry.use_request, kwargs)
            kwargs[entry.use_request] = request
        try:
            args, kwargs, ret_ann = self.construct_args(sig, kwargs)
            if asyncio.iscoroutinefunction(handler):
                ret = yield from handler(*args, **kwargs)
            else:
                ret = handler(*args, **kwargs)
            if ret_ann is not None:
                ret = ret_ann(ret)
        except aiohttp.HttpException as exc:
            raise
        except Exception as exc:
            # add log about error
            raise aiohttp.HttpErrorException(500,
                                             "Internal Server Error") from exc
        else:
            return json.dumps(ret)

    def construct_args(self, sig, kwargs):
        try:
            bargs = sig.bind(**kwargs)
        except TypeError:
            raise
        else:
            args = bargs.arguments
            for name, param in sig.parameters.items():
                if param.annotation is param.empty:
                    continue
                val = args.get(name, param.default)
                # NOTE: default value always being passed through annotation
                #       is it realy neccessary?
                try:
                    args[name] = param.annotation(val)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        'Invalid value for argument {!r}: {!r}'
                        .format(name, exc)) from exc
            if sig.return_annotation is not sig.empty:
                return bargs.args, bargs.kwargs, sig.return_annotation
            return bargs.args, bargs.kwargs, None