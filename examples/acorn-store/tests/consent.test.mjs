import {test} from "node:test";
import assert from "node:assert/strict";
import {setupConsent} from "../consent.mjs";

function elements() {
  const accept = {checked:false}, button = {disabled:false}, status = {textContent:""};
  const form = {hidden:false, addEventListener(_name, cb) {this.submit = cb;},
    querySelector(query) {return query === 'input[name="accept"]' ? accept : button;}};
  const root = {hidden:true, querySelector(query) {return query === "form" ? form : status;}};
  return {root,form,accept,button,status};
}
test("consent stays hidden if the authority cannot confirm an eligible session", async () => {
  const {root} = elements();
  await setupConsent({root,fetchImpl:async () => new Response(null,{status:503})});
  assert.equal(root.hidden,true);
});
test("explicit optional choice sends no purchase or recipient details and recovers on failure", async () => {
  const {root,form,accept,button,status} = elements();
  const calls = [];
  let grantSucceeds = false;
  const fetchImpl = async (url, options) => {
    calls.push({url,options});
    return url.endsWith("context") ? Response.json({available:true}) :
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
