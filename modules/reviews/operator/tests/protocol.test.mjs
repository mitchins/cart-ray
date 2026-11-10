import test from 'node:test';
import assert from 'node:assert/strict';
import {privateRequest} from '../protocol.mjs';
test('private JSON calls retain exact byte length required by the existing Reviews API',()=>{
 const input={service:'REVIEWS',path:'/reviews-internal/v1/automation-activate',method:'POST',headers:{authorization:'Bearer synthetic'},body:{policy_version:'v1',delay_seconds:0,max_lateness:3600}};
 const {url,init}=privateRequest(input);
 assert.equal(url,'https://operator.internal/reviews-internal/v1/automation-activate');
 assert.equal(init.headers.get('content-length'),String(Buffer.byteLength(JSON.stringify(input.body))));
 assert.equal(init.headers.get('authorization'),'Bearer synthetic');
 assert.deepEqual(JSON.parse(init.body),input.body);
 const unicode=privateRequest({...input,body:{body:'Squirrel’s review'}});
 assert.equal(unicode.init.headers.get('content-length'),String(Buffer.byteLength(unicode.init.body)));
});
test('private reads carry no body and caller cannot choose an arbitrary host or service',()=>{
 const read=privateRequest({service:'REVIEWS',path:'/reviews-admin/v1/pending'});
 assert.equal(read.init.body,undefined);
 assert.equal(read.init.headers.get('content-length'),null);
 for(const input of [{service:'OTHER',path:'/'},{service:'REVIEWS',path:'//attacker.test'},{service:'REVIEWS',path:'https://attacker.test'},{service:'REVIEWS',path:'/',method:'DELETE'}])assert.throws(()=>privateRequest(input));
});
