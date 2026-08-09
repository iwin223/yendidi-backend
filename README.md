# Y3ndidi Backend

This directory contains a FastAPI backend implementation for the Y3ndidi mobile app.

## Requirements

- Python 3.11+
- PostgreSQL 16

## Setup

1. Create a Python virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Create environment variables in `.env` or your shell:

```env
DATABASE_URL=postgresql+asyncpg://y3ndidi:changeme@localhost:5432/y3ndidi
JWT_SECRET=supersecret
PAYSTACK_SECRET_KEY=sk_test_yourkey
PAYSTACK_WEBHOOK_SECRET=whsec_yoursecret
ELASTICEMAIL_API_KEY=your_elasticemail_api_key
ELASTICEMAIL_SENDER=sender@example.com
```

3. Run the application:

```bash
uvicorn app.main:app --reload
```

## Alembic migrations

Run migrations with:

```bash
alembic upgrade head
```

## Notes

This implementation uses Paystack for all payment methods, including mobile money and bank transfer.
