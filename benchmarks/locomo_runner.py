#!/usr/bin/env python3
"""Mneme benchmark - fast model, 5 QA."""
import json, time, sys, requests

PROXY = "http://localhost:8080/v1/chat/completions"
OR = "https://openrouter.ai/api/v1/chat/completions"
OR_KEY = "$OR_KEY"
JUDGE = "openai/gpt-4o-mini"

def chat(msg, timeout=300):
    r = requests.post(PROXY, json={
        "model":"text-mneme:64k","messages":[{"role":"user","content":msg}],"stream":False
    }, timeout=timeout)
    return r.json()["choices"][0]["message"].get("content","") if r.ok else f"ERR{r.status_code}"

def judge(q, gt, ans):
    r = requests.post(OR, headers={"Authorization":f"Bearer {OR_KEY}","Content-Type":"application/json"},
        json={"model":JUDGE,"messages":[{"role":"user","content":f"Q:{q}\nGT:{gt}\nANS:{ans}\nReply: CORRECT/PARTIAL/WRONG"}],
              "max_tokens":10,"temperature":0}, timeout=30)
    txt = r.json()["choices"][0]["message"]["content"].strip().upper() if r.ok else "ERR"
    for l in ["CORRECT","PARTIAL","WRONG"]:
        if l in txt: return l
    return "?"

print("="*50)
print("Mneme Benchmark - LoCoMo (fast model)")
print("="*50)

with open("/workspace/LoCoMo/data/locomo10.json") as f:
    data = json.load(f)
conv = data[0]
summaries = conv.get("session_summary",{})
qa = conv["qa"][:5]

# Ingest 3 sessions
keys = sorted(summaries.keys())[:3]
for sk in keys:
    print(f"Ingesting {sk}...", end=" ", flush=True)
    t0 = time.time()
    r = chat(f"Ingest this conversation summary: {summaries[sk]}")
    print(f"({time.time()-t0:.0f}s)")
chat("<<SAVE>> Final save")
time.sleep(3)
print(f"  Saved. DB has {json.loads(requests.get('http://localhost:8080/health').text)['chunks']} chunks")

# QA
results = {"correct":0,"partial":0,"wrong":0}
for i, qa in enumerate(qa):
    q, gt = qa["question"], qa["answer"]
    print(f"Q{i+1}: {q[:80]}...", end=" ", flush=True)
    t0 = time.time()
    ans = chat(f"Based on past conversations, answer: {q}")
    elapsed = time.time()-t0
    j = judge(q, gt, ans)
    print(f"[{j}] ({elapsed:.0f}s)")
    if j=="CORRECT": results["correct"]+=1
    elif j=="PARTIAL": results["partial"]+=1
    else: results["wrong"]+=1

t = sum(results.values())
print(f"\nResults: {results['correct']}/{t} ({results['correct']/t:.1%}), {results['partial']} partial, {results['wrong']} wrong" if t else "No results")
