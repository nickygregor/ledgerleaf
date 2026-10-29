import http.client,json,tempfile,threading,unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from app import Ledger,handler

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(Ledger(Path(self.tmp.name)/'journal.db')))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,method,path,payload=None,headers=None):
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        hdr={'Content-Type':'application/json'};hdr.update(headers or {})
        client.request(method,path,None if payload is None else json.dumps(payload),hdr)
        response=client.getresponse();result=(response.status,response.read());client.close();return result
    def test_http_entry_export_and_delete(self):
        status,body=self.request('POST','/api/entries',{'day':'2026-10-29','merchant':'Test','category':'Learning','amount':'9.99','note':''})
        self.assertEqual(status,201);entry=json.loads(body)['id']
        status,body=self.request('GET','/api/export?month=2026-10');self.assertEqual(status,200);self.assertIn(b'9.99',body)
        self.assertEqual(self.request('POST','/api/delete',{'id':entry})[0],200)
        self.assertEqual(self.request('POST','/api/delete',{'id':entry})[0],404)
    def test_reject_foreign_host_and_origin(self):
        self.assertEqual(self.request('GET','/',headers={'Host':'untrusted.example'})[0],403)
        self.assertEqual(self.request('POST','/api/demo',{},headers={'Origin':'https://untrusted.example'})[0],403)
    def test_bad_payload_and_traversal(self):
        self.assertEqual(self.request('POST','/api/entries',[])[0],400)
        self.assertEqual(self.request('GET','/../../app.py')[0],404)
        self.assertEqual(self.request('GET','/api/report?month=nope')[0],400)
if __name__=='__main__':unittest.main()
