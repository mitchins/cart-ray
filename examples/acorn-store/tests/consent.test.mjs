import {test} from "node:test";
import assert from "node:assert/strict";
import {setupConsent, isPostPurchase} from "../consent.mjs";

function elements() {
  const accept = {checked:false}, button = {disabled:false}, status = {textContent:""};
  const form = {hidden:false, addEventListener(_name, cb) {this.submit = cb;},
    querySelector(query) {return query === 'input[name="accept"]' ? accept : button;}};
  const code = {value:""}, verify={hidden:true}, send={disabled:false,addEventListener(_name,cb){this.click=cb;}}, codeForm={hidden:true,addEventListener(_name,cb){this.submit=cb;},querySelector(){return code;}};
  const retry = {hidden:true, addEventListener(_name,cb){this.click=cb;}};
  const root = {hidden:true, querySelector(query) {return ({"#review-retry":retry,"#review-grant":form,"#review-verify":verify,"#review-code-form":codeForm,"#review-send-code":send})[query] || status;}};
  return {root,form,accept,button,status,code,verify,send,codeForm,retry};
}
test("consent stays hidden if the authority cannot confirm an eligible session", async () => {
  const {root} = elements();
  await setupConsent({root,fetchImpl:async () => new Response(null,{status:503})});
  assert.equal(root.hidden,true);
});
test("email verification exposes the grant only after a six-digit code succeeds", async () => {
  const {root,form,verify,send,codeForm,code}=elements(), calls=[];
  const fetchImpl=async(url,options)=>{calls.push([url,options.body]);
    return Response.json(url.endsWith("context") ? {available:true,verified:false} :
      url.endsWith("challenge") ? {sent:true} : {verified:true});};
  await setupConsent({root,fetchImpl}); assert.equal(verify.hidden,false); assert.equal(form.hidden,true);
  await send.click(); assert.equal(codeForm.hidden,false); code.value="123456";
  await codeForm.submit({preventDefault(){}}); assert.equal(verify.hidden,true); assert.equal(form.hidden,false);
  assert.deepEqual(calls.map(x=>x[1]),["{}","{}",'{"code":"123456"}']);
});
test("explicit optional choice sends no purchase or recipient details and recovers on failure", async () => {
  const {root,form,accept,button,status} = elements();
  const calls = [];
  let grantSucceeds = false;
  const fetchImpl = async (url, options) => {
    calls.push({url,options});
    return url.endsWith("context") ? Response.json({available:true,verified:true}) :
      grantSucceeds ? Response.json({recorded:true}) : new Response(null,{status:503});
  };
  await setupConsent({root,fetchImpl});
  assert.equal(root.hidden,false);
  const event = {preventDefault(){}};
  await form.submit(event);
  assert.equal(calls.length,1);
  accept.checked = true;
  await form.submit(event);
  assert.equal(calls[1].options.body,'{"accept":true}');
  assert.equal(button.disabled,false);
  assert.match(status.textContent,/couldn’t save/);
  grantSucceeds = true;
  await form.submit(event);
  assert.equal(form.hidden,true);
  assert.match(status.textContent,/saved/);
});

test("post-purchase failure offers a generic retry without exposing the consent form", async () => {
  const {root,form,verify,status,retry}=elements(); form.hidden=true; let reloaded=false;
  await setupConsent({root,postPurchase:true,reload:()=>{reloaded=true;},fetchImpl:async()=>new Response(null,{status:503})});
  assert.equal(root.hidden,false); assert.equal(form.hidden,true); assert.equal(verify.hidden,true);
  assert.match(status.textContent,/couldn’t load/); assert.equal(retry.hidden,false); retry.click(); assert.equal(reloaded,true);
});

test("checkout return marker survives URL cleanup and retry only in the same tab and time window", () => {
  const now=10000000, storage={getItem(key){return key === "acorn-review-return" ? String(now-1000) : null;}};
  assert.equal(isPostPurchase({pathname:"/",storage,now}),true);
  assert.equal(isPostPurchase({pathname:"/products/item/",storage,now}),false);
  assert.equal(isPostPurchase({pathname:"/",storage,now:now+7200000}),false);
  assert.equal(isPostPurchase({pathname:"/",storage:{getItem(){return null;}},now}),false);
  assert.equal(isPostPurchase({pathname:"/purchase-complete/",storage,now}),true);
});
