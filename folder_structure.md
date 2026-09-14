auto/
├── job_adapters/
│   ├── base.py                     ← abstract interface all portals must implement (login/search/apply)
│   ├── linkedin/
│   │   ├── login.py                ← loads linkedin_auth.json cookies, re-auths if expired
│   │   ├── search.py               ← searches jobs by title+city, filters Easy Apply only
│   │   └── apply.py                ← clicks Easy Apply modal, uploads resume, submits form
│   ├── naukri/
│   │   ├── login.py                ← email+password login with session persistence
│   │   ├── search.py               ← scrapes job cards by title+location
│   │   └── apply.py                ← fills naukri apply form, uploads tailored resume
│   ├── instahyre/
│   │   ├── login.py                ← email+password login with session persistence
│   │   ├── search.py               ← scrapes opportunity listings by title
│   │   └── apply.py                ← one-click apply with resume upload
│   └── workday/
│       ├── login.py                ← detects workday redirect, fires LinkedIn OAuth handshake
│       ├── form_filler.py          ← handles 15-step wizard, fixes resume auto-parse errors
│       └── session.py              ← saves/loads cookies per company domain (att, statestreet etc)
│
├── services/
│   ├── resume_tailor/
│   │   ├── jd_parser.py            ← deepseek-r1:14b extracts skills, keywords, seniority from JD
│   │   ├── resume_rewriter.py      ← qwen2.5:14b rewrites bullets to match JD without lying
│   │   └── docx_builder.py         ← python-docx injects rewritten sections into your .docx template
│   └── application_tracker/
│       └── service.py              ← thin wrapper over existing SQLAlchemy models, logs every application
│
├── api/
│   └── scraper.py                  ← FastAPI routes: /scraper/run, /scraper/status, /scraper/jobs
│
├── utils/
│   └── llm/
│       └── providers/
│           └── ollama.py           ← already exists, just needs OLLAMA_BASE_URL=host.containers.internal
│
└── resume_template/
    └── Rishi_Karan_Resume.docx     ← base template, never modified, always copied before tailoring
