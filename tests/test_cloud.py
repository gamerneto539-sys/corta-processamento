import json
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('corta_cloud_server',ROOT/'cloud/server.py')
cloud_server=importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_server)


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.token='x'*40
        self.server=cloud_server.create_server(self.folder.name,self.token,host='127.0.0.1',port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown();self.thread.join();self.server.sessions.close();self.server.server_close();self.folder.cleanup()

    def request(self,path,owner='a'*64,token=None,body=None):
        headers={'Authorization':'Bearer '+(self.token if token is None else token),'X-Corta-Owner':owner}
        if body is not None:headers['Content-Type']='application/json'
        req=urllib.request.Request(self.base+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req,timeout=10) as response:return response.status,response.headers,response.read()
        except urllib.error.HTTPError as error:return error.code,error.headers,error.read()

    def test_authorization_and_public_probe(self):
        self.assertEqual(self.request('/healthz',token='wrong')[0],200)
        self.assertEqual(self.request('/api/jobs',token='wrong')[0],401)
        self.assertEqual(self.request('/api/jobs',owner='../escape')[0],400)

    def test_user_history_and_files_are_isolated(self):
        alice=self.server.sessions.get('a'*64)
        jobid='b'*32;folder=alice.downloads/jobid;folder.mkdir();(folder/'Song.mp3').write_bytes(b'real-attachment-bytes')
        alice.jobs[jobid]={'id':jobid,'status':'completed','created_at':time.time(),'filename':'Song.mp3'}
        self.assertEqual(self.request('/api/files/'+jobid)[2],b'real-attachment-bytes')
        self.assertEqual(self.request('/api/files/'+jobid,owner='b'*64)[0],400)
        self.assertEqual(json.loads(self.request('/api/jobs',owner='b'*64)[2])['jobs'],[])
        self.assertIn('attachment',self.request('/api/files/'+jobid)[1]['Content-Disposition'])

    def test_expired_results_are_deleted(self):
        alice=self.server.sessions.get('a'*64);jobid='c'*32;folder=alice.downloads/jobid;folder.mkdir();(folder/'Song.mp3').write_bytes(b'expired')
        alice.jobs[jobid]={'id':jobid,'status':'completed','created_at':time.time()-7200,'filename':'Song.mp3'}
        self.server.sessions.prune()
        self.assertFalse(folder.exists());self.assertEqual(alice.jobs[jobid]['status'],'error')

    def test_cloud_rejects_local_actions_and_external_urls(self):
        self.assertEqual(self.request('/api/open-downloads',body={})[0],404)
        self.assertEqual(self.request('/api/analyze',body={'url':'http://127.0.0.1/private'})[0],400)


if __name__=='__main__':unittest.main()
