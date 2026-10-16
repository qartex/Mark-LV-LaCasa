import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from dashboard.server import DashboardServer


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.server = DashboardServer(public_url='https://lacasa-server.tail7a9c2.ts.net', host='127.0.0.1')
        self.client = TestClient(self.server.app)
        self.key = self.server.new_key()
        self.token = self.client.post('/login', json={'pin':self.key}).json()['token']
        self.headers = {'Authorization':f'Bearer {self.token}'}

    def test_auth_status_interrupt_and_command(self):
        cb = Mock()
        self.server.set_interrupt_callback(cb)
        for route, method in [('/api/status','get'),('/api/interrupt','post'),('/api/command','post')]:
            self.assertEqual(getattr(self.client,method)(route, json={} if method=='post' else None).status_code if method=='post' else self.client.get(route).status_code,401)
        self.assertEqual(self.client.get('/api/status',headers=self.headers).json()['audio_allowed'],False)
        self.server.set_audio_allowed_callback(lambda:True)
        self.assertTrue(self.client.get('/api/status',headers=self.headers).json()['audio_allowed'])
        self.assertEqual(self.client.post('/api/interrupt',headers=self.headers).status_code,200)
        cb.assert_called_once()
        self.assertEqual(self.client.post('/api/command',headers=self.headers,json={'text':'hello'}).status_code,200)
        self.assertEqual(self.server._command_queue.get_nowait(),'hello')

    def test_invalid_expired_and_rotation_revokes_every_credential(self):
        self.assertEqual(self.client.post('/login',json={'pin':'WRONG'}).status_code,401)
        expired=self.server.new_key(expiry_secs=-1)
        self.assertEqual(self.client.post('/login',json={'pin':expired}).status_code,401)
        key=self.server.new_key()
        html=self.client.get('/auto-login',params={'key':key}).text
        import re
        device=re.search("jarvis_device_token','([^']+)",html).group(1)
        self.assertEqual(self.client.post('/api/device-login',json={'device_token':device}).status_code,200)
        self.server.new_key()
        self.assertEqual(self.client.get('/api/status',headers=self.headers).status_code,401)
        self.assertEqual(self.client.post('/api/device-login',json={'device_token':device}).status_code,401)
        self.assertEqual(self.client.post('/login',json={'pin':key}).status_code,401)
        self.assertEqual(self.server.get_url(),'https://lacasa-server.tail7a9c2.ts.net')

    def test_websocket_current_status_and_auth(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/ws?token=bad'):pass
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/ws/phone-audio?token=bad'):pass
        with self.client.websocket_connect(f'/ws?token={self.token}') as ws:
            self.assertEqual(ws.receive_json()['type'],'status')
            ws.send_json({'type':'command','text':'hi'})
        self.assertEqual(self.server._command_queue.get_nowait(),'hi')

    def test_single_source_gate_end_order_and_stale_frame(self):
        self.server.set_audio_allowed_callback(lambda:True)
        url=f'/ws/phone-audio?token={self.token}'
        with self.client.websocket_connect(url) as ws:
            self.assertEqual(ws.receive_json()['type'],'audio_ready')
            with self.assertRaises(WebSocketDisconnect) as second:
                with self.client.websocket_connect(url):pass
            self.assertEqual(second.exception.code,4009)
            ws.send_bytes(b'\x01\x02')
            ws.send_json({'type':'audio_stream_end'})
            self.assertEqual(ws.receive_json()['type'],'audio_ended')
        pcm=self.server._phone_audio_queue.get_nowait()
        end=self.server._phone_audio_queue.get_nowait()
        self.assertEqual(pcm['mime_type'],'audio/pcm;rate=16000')
        self.assertTrue(end['audio_stream_end'])
        self.assertTrue(self.server.audio_message_valid(pcm))
        self.server.invalidate_audio()
        self.assertFalse(self.server.audio_message_valid(pcm))
        self.server.set_audio_allowed_callback(lambda:False)
        with self.assertRaises(WebSocketDisconnect) as busy:
            with self.client.websocket_connect(url):pass
        self.assertEqual(busy.exception.code,4009)

    def test_gate_rechecked_on_existing_stream(self):
        allowed=[True]
        self.server.set_audio_allowed_callback(lambda:allowed[0])
        with self.client.websocket_connect(f'/ws/phone-audio?token={self.token}') as ws:
            ws.receive_json()
            allowed[0]=False
            ws.send_bytes(b'\x00\x01')
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()
        self.assertTrue(self.server._phone_audio_queue.empty())

    def test_rotation_closes_real_control_and_audio_websockets(self):
        self.server.set_audio_allowed_callback(lambda:True)
        with TestClient(self.server.app) as client:
            with client.websocket_connect(f'/ws?token={self.token}') as control:
                control.receive_json()
                with client.websocket_connect(f'/ws/phone-audio?token={self.token}') as audio:
                    audio.receive_json()
                    client.portal.call(self.server.new_key)
                    # Already-sent history may precede the close control frame.
                    with self.assertRaises(WebSocketDisconnect):
                        for _ in range(4):
                            control.receive_json()
                    with self.assertRaises(WebSocketDisconnect):
                        audio.receive_json()
        self.assertEqual(self.client.get('/api/status',headers=self.headers).status_code,401)


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_pair_cli_targets_running_instance_and_rotates_live_sockets(self):
        from core.headless import request_pair
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private'/'control.sock'
            server=DashboardServer(public_url='https://lacasa-server.tail7a9c2.ts.net')
            ws=Mock()
            async def close(**kwargs):pass
            ws.close=Mock(side_effect=close)
            server._clients.add(ws)
            task=asyncio.create_task(server.serve_control(path))
            try:
                for _ in range(100):
                    if path.exists():break
                    await asyncio.sleep(.01)
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                url=await asyncio.to_thread(request_pair,path)
                self.assertTrue(url.startswith(server.get_url()+'/auto-login?key='))
                key=url.split('key=')[1]
                self.assertIn(key,server._pending_keys)
                self.assertEqual(ws.close.call_count,1)
                await asyncio.to_thread(request_pair,path)
                self.assertNotIn(key,server._pending_keys)
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)


class BrowserVoiceTests(unittest.TestCase):
    def test_permission_race_disconnect_speaking_and_explicit_stop(self):
        import subprocess
        html = (Path(__file__).resolve().parents[1]/'dashboard/static/app.html').read_text()
        script = r'''
const vm = require('vm'), assert = require('assert');
let html = require('fs').readFileSync(0, 'utf8');
assert(!html.includes('chrome://flags'));
const elements = new Map();
function element(id) {
 if(!elements.has(id)) elements.set(id, {innerHTML:'',textContent:'',value:'',disabled:false,
 classList:{add(){},remove(){}},addEventListener(){},appendChild(){},style:{},querySelector(){return element('child');}});
 return elements.get(id);
}
let sockets = [], resolvePermission, stopped=0, requests=[];
class WS {
 constructor(url){this.url=url;this.readyState=0;this.sent=[];sockets.push(this);}
 send(value){this.sent.push(value);}
 close(){this.readyState=3;}
}
class AC {
 constructor(){this.state='running';this.sampleRate=16000;this.destination={};}
 resume(){return Promise.resolve();} close(){this.closed=true;return Promise.resolve();}
 createMediaStreamSource(){return {connect(){},disconnect(){}};}
 createScriptProcessor(){return {connect(){},disconnect(){}};}
}
const context = vm.createContext({console,WebSocket:WS,AudioContext:AC,Blob,URL,
 Float32Array,Int16Array,Uint8Array,Math,JSON,Promise,Date,
 document:{getElementById:element,createElement:()=>element(Math.random())},
 sessionStorage:{getItem:k=>k==='jarvis_token'?'token':null},
 location:{protocol:'https:',host:'private.example',origin:'https://private.example',replace(){}},
 navigator:{mediaDevices:{getUserMedia:()=>new Promise(resolve=>resolvePermission=resolve)}},
 fetch:async (url,opts)=>{requests.push(url);return {ok:true,json:async()=>({type:'status',state:'active',audio_allowed:true})};},
 setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){},
 addEventListener(){},window:{addEventListener(){}},btoa:s=>Buffer.from(s).toString('base64')});
vm.runInContext([...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n'),context);
const control=sockets[0];
function status(allowed){control.onmessage({data:JSON.stringify({type:'status',state:'active',audio_allowed:allowed})});}
const tick=()=>new Promise(r=>setImmediate(r));
function stream(){return {getTracks:()=>[{stop(){stopped++;}}]};}
(async()=>{
 status(true);
 let pending=vm.runInContext('doMic()',context);
 status(false);resolvePermission(stream());await pending;await tick();
 assert.equal(stopped,1,'late permission must immediately stop tracks');
 assert.equal(sockets.length,1,'gate changed: no voice connection');
 status(true);pending=vm.runInContext('doMic()',context);resolvePermission(stream());await pending;
 let voice=sockets.at(-1);voice.onerror();await tick();
 assert.equal(stopped,2,'failed connection closes mic');
 status(true);pending=vm.runInContext('doMic()',context);resolvePermission(stream());await pending;
 voice=sockets.at(-1);status(false);await tick();
 assert.equal(stopped,3,'speaking status closes mic');
 status(true);await tick();assert.equal(stopped,3,'idle never starts mic automatically');
 pending=vm.runInContext('doMic()',context);resolvePermission(stream());await pending;
 voice=sockets.at(-1);voice.readyState=1;
 await vm.runInContext('doMic()',context);await tick();
 assert(voice.sent.some(x=>typeof x==='string'&&JSON.parse(x).type==='audio_stream_end'),'explicit mic stop sends end');
 await vm.runInContext('doStop()',context);await tick();
 assert(requests.includes('/api/interrupt'),'Stop uses authenticated control route');
 console.log('browser voice lifecycle passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node','-e',script],input=html,text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
