#!/usr/bin/env node
// Operator workstation only. Never bundled into a Worker.
import {readFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {doctor,verifyInstallation,sourceIdentityURL} from '../modules/reviews/automation-intake-producer/operator.mjs';
const [mode,configPath,revision]=process.argv.slice(2);
if (!['doctor','verify'].includes(mode) || !configPath) throw Error('usage: node scripts/reviews_installation.mjs doctor|verify operator.json [expected-revision]');
const config=JSON.parse(await readFile(configPath,'utf8'));
const env={...config,...Object.fromEntries(['CF_READ_TOKEN','SOURCE_IDENTITY_TOKEN'].map(k=>[k,process.env[k]]))};
// Explicit authenticated CF token stays in this operator process only.
if (!env.CF_READ_TOKEN) throw Error('operator CF_READ_TOKEN required');
const identityURL=await sourceIdentityURL(env);
const execute=(sql,args=[])=>{
  const quote=x=>typeof x==='number' ? String(x) : "'"+String(x).replaceAll("'","''")+"'";
  let i=0;const query=sql.replaceAll('?',()=>quote(args[i++]));
  const result=JSON.parse(execFileSync('npx',['wrangler','d1','execute',env.SOURCE_DATABASE_ID,'--remote','--json','--command',query],{encoding:'utf8',env:{...process.env,CLOUDFLARE_API_TOKEN:env.CF_READ_TOKEN,CLOUDFLARE_ACCOUNT_ID:env.CF_ACCOUNT_ID}}));
  if (!Array.isArray(result) || result.some(r=>r.success!==true)) throw Error('D1 operator operation failed');
  return result[0];
};
env.COMMERCE_DB={withSession(){return this},prepare(sql){let args=[];return {bind(...values){args=values;return this},async first(){return execute(sql,args).results?.[0]??null},async all(){return execute(sql,args)}}}};
env.SOURCE_API={fetch(_url,options){return fetch(identityURL,{...options,redirect:'error'})}};
// Inspection requires these bindings only as capability markers; never calls activation or queue send.
env.REVIEWS_API={};env.EVENT_QUEUE={};
console.log(JSON.stringify(mode==='doctor' ? await doctor(env) : await verifyInstallation(env,Number(revision)),null,2));
