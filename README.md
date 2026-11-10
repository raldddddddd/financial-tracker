# Personal Shopee expense tracker

A local Python MVP: Gmail search → MIME text/HTML → Gemini structured JSON → SQLite → dashboard. It only writes extracted purchase fields locally; it does not save raw email bodies. The example orders are synthetic, based on the two amounts described in the referenced conversation (₱1,025 and ₱1,072, combined ₱2,097). The screenshots were unavailable, so sample IDs and product labels are illustrative.

## Run the sample first

Use Python 3.11 or newer:

```bash
cd /path/to/shopee-tracker
python3 -m tracker demo
python3 -m tracker serve
```

Open `http://127.0.0.1:8765`. The dashboard binds only to your computer. `data/expenses.sqlite3` is created locally. To clear only the demo data, delete that database before connecting Gmail.

## Connect your Gmail account

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/), then enable **Gmail API**.
  2. Open **Google Auth Platform → Branding**. If prompted, click **Get started**, enter the app name and contact emails, select **External** under **Audience** for a personal Gmail account, and complete the setup. Then open **Audience** in the left menu. Check that **Publishing status** says **Testing** (it is a status, not a choice in the setup wizard), and under **Test users** add your Gmail address.
3. Open **Google Auth Platform → Data Access** in the left menu, click **Add or remove scopes**, and select `https://www.googleapis.com/auth/gmail.readonly` (shown as **View your email messages and settings**). If it is absent, confirm that the Gmail API is enabled for the same Cloud project, or add the full scope URL manually. Click **Update**, then **Save**.
4. In **Clients**, create an OAuth client of type **Desktop app**. Put its downloaded JSON at `.secrets/credentials.json` inside this project. The `.secrets` folder is already present locally. Do not paste the JSON into chat or commit it.
5. Open the local `.env` file and replace `you@gmail.com` with your Gmail address and `replace-locally` on the `GEMINI_API_KEY` line with a key from [Google AI Studio](https://aistudio.google.com/app/apikey). If `.env` is missing in a fresh copy, copy `.env.example` to `.env` and run `chmod 600 .env`. The default `gemini-3.8-flash` is listed with a free tier as of November 2026; check [current model pricing](https://ai.google.dev/gemini-api/docs/pricing) and your project's quota before syncing. Free-tier Gemini data may be used to improve Google's products.
6. Install and authorize:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m tracker auth
python -m tracker sync
python -m tracker serve
```

In a new terminal session, activate `.venv` again or run commands directly with `./.venv/bin/python`, for example `./.venv/bin/python -m tracker sync`.

The first sync may take a while for a large mailbox. It spaces Gmail requests to stay within the per-user quota and retries temporary rate-limit errors. Each processed email is saved immediately, so rerunning the same command resumes without parsing those emails again. Refresh the dashboard after sync completes.

On first authorization, choose the same Google account as `GMAIL_ACCOUNT`. The app checks the authorized account before syncing. OAuth tokens are stored in `.secrets/token.json` with owner-only file permissions. Set `.secrets/credentials.json` to owner-only permissions too (`chmod 600 .secrets/credentials.json`). `.env`, `.secrets/`, and `data/` are Git ignored. Never commit any of them.

**Gmail scope:** `gmail.readonly` can read all messages and settings in the authorized mailbox. It is **not** limited to Shopee by Google. The app searches Shopee-related subjects, checks the sender domain, and processes only matching messages. The default search can be changed through `SHOPEE_QUERY` if actual confirmation subjects differ; keep the domain check aligned with `SHOPEE_SENDER_DOMAIN`. The search also includes refund and cancellation subjects. It does not process promotions unless they pass the confirmation checks.

**Testing-mode OAuth:** Google may expire refresh tokens after seven days for an External app in Testing, so you may need to authorize again. Remove `.secrets/token.json` and run `python -m tracker auth` if the saved token no longer works. See [Google's OAuth audience guidance](https://support.google.com/cloud/answer/15549945).

## How accounting works

- Each order ID is unique in SQLite. A re-run skips processed Gmail IDs; later order events update the same order. Older emails cannot overwrite newer state.
- The per-order paid amount is the expense source of truth. A combined payment is stored only for reconciliation, never counted again.
- Cancelled and fully refunded orders count as zero. Partial refunds subtract the stated refund amount. Missing partial-refund amounts appear in **Needs review**.
- Item categories split each order's net paid amount in proportion to the listed item line prices. This keeps category totals equal to net spending, including shipping and discounts. If item prices are unavailable, the amount is **Unallocated**.
- Mismatched combined totals, item arithmetic, excessive refunds, and missing order IDs are flagged or rejected. Failed emails can be retried on the next sync. Check **Needs review** before relying on totals.

The app strips likely address, phone and email details from receipt text before the Gemini call. It does not keep the raw receipt, but redaction is heuristic. Review the code before using it with sensitive mail. Gmail and Gemini API calls require internet access. The free-tier model and quotas may change.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use synthetic fixtures and do not access Gmail or Gemini.

Reference documentation: [Gmail Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python), [Gmail message search and full retrieval](https://developers.google.com/workspace/gmail/api/guides/list-messages), [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output).
