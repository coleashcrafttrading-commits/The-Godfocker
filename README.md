# 🦋 Butterfly Bot

A dead-simple local dashboard that opens and closes an **interlocking 3-rung Iron
Butterfly ladder** on **SPY (0DTE)** through your **Alpaca** account with one click.

- **▲ OPEN LADDER** — submits 3 net-credit limit combo orders (12 legs total):
  one Iron Butterfly centered at the ATM strike, one a strike below, one a strike
  above. Each rung sells a call + put at its center and buys a call/put wing
  `wing_width` away.
- **■ CLOSE ALL OPTIONS** — market-closes every open option position to get you flat.
- Live preview of strikes, mids, and the estimated credit before you click.
- Editable presets (wing width, center spacing, DTE, quantity).
- Defaults to your **paper** account. Going live is a deliberate one-line change.

> ⚠️ Alpaca does **not** trade SPX/NDX index options — only stock & ETF options.
> This bot uses **SPY** as the S&P 500 proxy.

---

## 1. Add your keys

Copy the example env file and fill it in (the real `.env` is git-ignored):

```powershell
copy .env.example .env
notepad .env
```

Fill in:
- `ALPACA_API_KEY` / `ALPACA_API_SECRET` — your **paper** keys from
  https://app.alpaca.markets (Paper account → API Keys).
- `ALPACA_BASE_URL` — leave as the paper URL for now.
- `SUPABASE_URL` / `SUPABASE_ANON_KEY` — optional (for the trade log). Leave blank to skip.
- `DASHBOARD_PASSWORD` — set a real password to require it before any trade button works.

## 2. (Optional) Set up the Supabase trade log

In your Supabase project: **SQL Editor → New query**, paste the contents of
[`supabase_schema.sql`](supabase_schema.sql), and run it. Then put the project URL +
anon key in `.env`. Skip this entirely and the bot still works — it just won't log.

## 3. Run it

Double-click **`run.bat`**, or:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000**.

---

## How the order is built

| Rung | Sells (center) | Buys (wings) |
|------|----------------|--------------|
| ATM − spacing | call + put @ C₁ | call @ C₁+wing, put @ C₁−wing |
| ATM           | call + put @ C₂ | call @ C₂+wing, put @ C₂−wing |
| ATM + spacing | call + put @ C₃ | call @ C₃+wing, put @ C₃−wing |

Each rung is submitted as a **single 4-leg multi-leg (MLEG) limit order** at the
computed net credit (mid of each leg, minus an optional `limit_shade` to help fills).
Quantity is per rung.

## Going live (later, on purpose)

When you've watched it behave in paper:
1. In `.env`, set `ALPACA_BASE_URL=https://api.alpaca.markets`
2. Swap in your **live** API key/secret.
3. Restart. The badge in the top-left turns red and says **LIVE**.

## Safety notes

- Limit (not market) entries by default, so you won't get a terrible fill on the open.
- The close button uses market orders so it reliably gets you flat — on a 0DTE you
  usually want *out* more than you want a perfect price.
- 0DTE only exists on trading days. On a weekend/holiday the open will error clearly.
- This is your money. Test in paper until you trust it.
