"""
    The base logic was taken from project https://github.com/squachen/micloud

    I had to rewrite the code to work asynchronously and handle timeouts for
    requests to the cloud.

    MIT License

    Copyright (c) 2020 Sammy Svensson

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import string
import time

from aiohttp import ClientSession, ClientConnectorError

_LOGGER = logging.getLogger(__name__)

SERVERS = ['cn', 'de', 'i2', 'ru', 'sg', 'us']
UA = "Android-7.1.1-1.0.0-ONEPLUS A3010-136-%s APP/xiaomi.smarthome APPV/62830"


class MiCloud:
    auth = None
    svr = None
    _fail_count = 0

    def __init__(self, session: ClientSession):
        self.session = session
        self.device_id = get_random_string(16)

    async def login(self, username: str, password: str):
        try:
            payload = await self._login_step1()
            data = await self._login_step2(username, password, payload)
            if not data['location']:
                return False

            token = await self._login_step3(data['location'])

            self.auth = {
                'user_id': data['userId'],
                'ssecurity': data['ssecurity'],
                'service_token': token
            }

            return True

        except Exception as e:
            _LOGGER.exception(f"Can't login to Mi Cloud: {e}")
            return False

    def login_by_credientals(self, userId, serviceToken, ssecurity):
        self.auth = {
            'user_id': userId,
            'ssecurity': ssecurity,
            'service_token': serviceToken
        }

        return True

    async def _login_step1(self):
        r = await self.session.get(
            'https://account.xiaomi.com/pass/serviceLogin',
            cookies={'sdkVersion': '3.8.6', 'deviceId': self.device_id},
            headers={'User-Agent': UA % self.device_id},
            params={'sid': 'xiaomiio', '_json': 'true'})
        raw = await r.read()
        _LOGGER.debug(f"MiCloud step1")
        resp: dict = json.loads(raw[11:])
        return {k: v for k, v in resp.items()
                if k in ('sid', 'qs', 'callback', '_sign')}

    async def _login_step2(self, username: str, password: str, payload: dict):
        payload['user'] = username
        payload['hash'] = hashlib.md5(password.encode()).hexdigest().upper()

        r = await self.session.post(
            'https://account.xiaomi.com/pass/serviceLoginAuth2',
            cookies={'sdkVersion': '3.8.6', 'deviceId': self.device_id},
            data=payload,
            headers={'User-Agent': UA % self.device_id},
            params={'_json': 'true'})
        raw = await r.read()
        _LOGGER.debug(f"MiCloud step2")
        resp = json.loads(raw[11:])
        return resp

    async def _login_step3(self, location):
        r = await self.session.get(location, headers={'User-Agent': UA})
        service_token = r.cookies['serviceToken'].value
        _LOGGER.debug(f"MiCloud step3")
        return service_token

    async def get_total_devices(self, servers: list):
        total = []
        for server in servers:
            devices = await self.get_devices(server)
            if devices is None:
                return None
            total += devices
        return total

    async def get_devices(self, server: str):
        assert server in SERVERS, "Wrong server: " + server
        baseurl = 'https://api.io.mi.com/app' if server == 'cn' \
            else f"https://{server}.api.io.mi.com/app"

        url = '/home/device_list'
        data = '{"getVirtualModel":false,"getHuamiDevices":0}'

        nonce = gen_nonce()
        signed_nonce = gen_signed_nonce(self.auth['ssecurity'], nonce)
        signature = gen_signature(url, signed_nonce, nonce, data)

        try:
            r = await self.session.post(baseurl + url, cookies={
                'userId': self.auth['user_id'],
                'serviceToken': self.auth['service_token'],
                'locale': 'en_US'
            }, headers={
                'User-Agent': UA,
                'x-xiaomi-protocal-flag-cli': 'PROTOCAL-HTTP2'
            }, data={
                'signature': signature,
                '_nonce': nonce,
                'data': data
            }, timeout=5)

            resp = await r.json(content_type=None)
            assert resp['code'] == 0, resp
            return resp['result']['list']

        except asyncio.TimeoutError:
            _LOGGER.error("Timeout while loading MiCloud device list")
        except:
            _LOGGER.exception(f"Can't load devices list")

        return None

    async def request_miot_api(self, api, params = None, server: str = None):
        server = server or self.svr or 'cn'
        api_base = 'https://api.io.mi.com/app' if server == 'cn' \
            else f"https://{server}.api.io.mi.com/app"
        url2 = "/miotspec/"
        url = api_base+url2+api

        nonce = gen_nonce()
        signed_nonce = gen_signed_nonce(self.auth['ssecurity'], nonce)
        signature = gen_signature(url2+api, signed_nonce, nonce, params)
        headers = {
            'content-type': "application/x-www-form-urlencoded",
            'x-xiaomi-protocal-flag-cli': "PROTOCAL-HTTP2",
            'connection': "Keep-Alive",
            'accept-encoding': "gzip",
            'cache-control': "no-cache",
        }
        try:
            r = await self.session.post(url, cookies={
                'userId': self.auth['user_id'],
                'serviceToken': self.auth['service_token'],
            }, headers={
                'User-Agent': UA,
                'x-xiaomi-protocal-flag-cli': 'PROTOCAL-HTTP2'
            }, data={
                'signature': signature,
                '_nonce': nonce,
                'data': params
            }, timeout=5)

            self._fail_count = 0
            resp = await r.json(content_type=None)
            if resp.get('message') == 'auth err':
                _LOGGER.error("小米账号登录信息失效")
                return None
            elif resp.get('code') != 0:
                _LOGGER.error(f"Response of {api} from cloud: {resp}")
                return resp
            else:
                # 注意：此处成功只代表请求是成功的，但控制设备不一定成功，
                # 取决于 result 里的 code
                _LOGGER.info(f"Response of {api} from cloud: {resp}")
                return resp

        except (asyncio.TimeoutError, ClientConnectorError) as ex:
            if self._fail_count < 3 and api == "prop/get":
                self._fail_count += 1
                _LOGGER.info(f"Error while requesting MIoT api {api} : {ex} ({self._fail_count})")
            else:
                _LOGGER.error(f"Error while requesting MIoT api {api} : {ex}")
        except:
            _LOGGER.exception(f"Can't request MIoT api")

    async def get_props(self, params: str = "", server: str = None):
        return await self.request_miot_api('prop/get', params, server)

    async def set_props(self, params: str = "", server: str = None):
        return await self.request_miot_api('prop/set', params, server)

    async def call_action(self, params: str = "", server: str = None):
        return await self.request_miot_api('action', params, server)


def get_random_string(length: int):
    seq = string.ascii_uppercase + string.digits
    return ''.join((random.choice(seq) for _ in range(length)))


def gen_nonce() -> str:
    """Time based nonce."""
    nonce = os.urandom(8) + int(time.time() / 60).to_bytes(4, 'big')
    return base64.b64encode(nonce).decode()


def gen_signed_nonce(ssecret: str, nonce: str) -> str:
    """Nonce signed with ssecret."""
    m = hashlib.sha256()
    m.update(base64.b64decode(ssecret))
    m.update(base64.b64decode(nonce))
    return base64.b64encode(m.digest()).decode()


def gen_signature(url: str, signed_nonce: str, nonce: str, data: str) -> str:
    """Request signature based on url, signed_nonce, nonce and data."""
    sign = '&'.join([url, signed_nonce, nonce, 'data=' + data])
    signature = hmac.new(key=base64.b64decode(signed_nonce),
                         msg=sign.encode(),
                         digestmod=hashlib.sha256).digest()
    return base64.b64encode(signature).decode()
