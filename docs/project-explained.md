# OpsPilot AI: Simple Explanation (Hinglish)

## OpsPilot AI kya hai?

Jab production me kuch toot jaye (server down, errors badh gaye), tab engineers ko raat ko jaagkar logs dekhne padte hain, problem dhundhni padti hai aur fix likhna padta hai. **OpsPilot AI ye kaam AI se aasaan banata hai.** Ye ek multi-tenant incident-response platform hai. Multi-tenant ka matlab hai ki ek hi system me kai companies (workspaces) alag-alag chal sakti hain.

## Core idea (ek line me)

> **Incident aaya → AI investigate karta hai (logs, metrics, code, runbooks) → evidence ke saath fix propose karta hai → insaan approve karta hai → tabhi GitHub par fix PR banta hai.**

Sabse zaroori rule ye hai ki **AI kabhi khud merge, deploy ya production change nahi karta.** Wo sirf approval ke baad ek fix branch, ek commit aur ek PR banata hai. Human hamesha loop me rehta hai.

## Kaise kaam karta hai (flow)

1. **Signals aate hain.** Signed webhooks aur Kafka-compatible broker se logs, metrics aur alarms aate hain. Inhe normalize aur de-duplicate karke incident bana diya jata hai.
2. **AI investigation.** LangGraph se bana AI agent worker me chalta hai. Wo read-only tools use karta hai: logs, metrics, GitHub code, recent commits, deployments aur runbooks.
3. **Runbook RAG.** Company ke docs (text, Markdown, PDF) chunk hote hain. Phir PostgreSQL full-text search aur pgvector (embeddings) se hybrid search hoti hai. Jawab ke saath citations bhi milte hain.
4. **Remediation (Module 6).** Evidence ke basis par patch generate hota hai. Phir policy check hota hai aur sandbox me test hota hai. Uske baad insaan approve karta hai aur GitHub PR banta hai.
5. **Security (Module 7).** Login OIDC se hota hai (AWS Cognito compatible). Roles aur permissions (RBAC), scoped API keys, rate limiting aur ek tamper-proof audit log hai.

## Kya-kya use hua (tech stack)

| Hissa | Technology | Kaam |
|---|---|---|
| Frontend | **Next.js**, Tailwind, TanStack Query | Dashboard, incidents, live updates |
| Backend API | **FastAPI** (Python), Pydantic | REST + WebSocket API |
| Background worker | **Redis + RQ** | Lambe AI jobs alag process me chalte hain |
| AI orchestration | **LangGraph** | Investigation ke steps, checkpoints ke saath |
| Database | **PostgreSQL + pgvector** | Saara data, full-text aur vector search |
| Migrations | **Alembic** | Database schema changes |
| Messaging | **Kafka-compatible broker** | Production signals ingest karna |
| Integration | **GitHub App** | Code padhna, aur approval ke baad PR banana |
| Auth | **OIDC / Cognito** | Login aur access control |
| Infra | **Docker Compose** | Poora stack ek command se chalta hai |
| Quality | pytest, Vitest, strict MyPy/TypeScript, zod | Testing aur type safety |

## Architecture simple me

```
Browser → Next.js (UI)
Browser → FastAPI (API) → Postgres + Redis
                              ↓
                        Worker → LangGraph AI → Postgres
```

Ye ek **modular monolith** hai. Code ek hi hai, aur API aur worker do alag process me chalte hain. Microservices nahi banaye gaye, kyunki abhi unki zarurat nahi thi.

## Project ki khaas baatein

- **Safety-first AI.** AI read-only hai, aur har write ke liye human approval chahiye.
- **Evidence-backed.** Har proposal ke saath proof aur citations hote hain.
- **Fail loudly.** Config galat ho ya dependency down ho, to system chhupata nahi hai. Wo error dikhata hai.
- **Module-wise build.** Module 0 se 7 tak step by step banaya gaya hai. Basic foundation se shuru hua, phir incidents, RAG, GitHub, observability, remediation aur auth aaye.
- **LLMOps evaluation.** Real-model evaluation aur retrieval evaluation reports bhi hain, jinse AI ki quality measure hoti hai.
