# Personal shop expense tracker

A local Python MVP: Gmail search → MIME text/HTML → Gemini structured JSON → SQLite → dashboard. It only writes extracted purchase fields locally; it does not save raw email bodies. The example orders are synthetic, based on two amounts (₱1,025 and ₱1,072, totaling ₱2,097). Sample IDs and product labels are illustrative.

## Run the sample first

Use Python 3.11 or newer:

```bash
cd /path/to/shopee-tracker
python3 -m tracker demo
python3 -m tracker serve
```

Open `http://127.0.0.1:8765`. The dashboard binds only to your computer. Use the shop tabs, year selector, or inclusive custom date range to update net spending, orders, recorded refunds, category totals, trend, and order list together. **All time** is the default. The trend groups All time by year, a selected year by month, and custom ranges by day, month, or year based on their length. Dates use Asia/Manila time. Each order's current net amount and linked refunds belong to its original purchase period, even if a refund arrived later. Approved Shopee refund requests without a linked purchase order remain in the global review area and are excluded from period totals. The Orders table shows one **Total** amount per order after refunds; recorded linked refund amounts appear in **Refunds**. Each order displays its shop; Shopee coins appear only on Shopee orders. `data/expenses.sqlite3` is created locally. To clear only the demo data, delete that database before connecting Gmail.

## Connect your Gmail account

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/), then enable **Gmail API**.
  2. Open **Google Auth Platform → Branding**. If prompted, click **Get started**, enter the app name and contact emails, select **External** under **Audience** for a personal Gmail account, and complete the setup. Then open **Audience** in the left menu. Check that **Publishing status** says **Testing** (it is a status, not a choice in the setup wizard), and under **Test users** add your Gmail address.
3. Open **Google Auth Platform → Data Access** in the left menu, click **Add or remove scopes**, and select `https://www.googleapis.com/auth/gmail.readonly` (shown as **View your email messages and settings**). If it is absent, confirm that the Gmail API is enabled for the same Cloud project, or add the full scope URL manually. Click **Update**, then **Save**.
4. In **Clients**, create an OAuth client of type **Desktop app**. Put its downloaded JSON at `.secrets/credentials.json` inside this project. The `.secrets` folder is already present locally. Do not paste the JSON into chat or commit it.
5. Open the local `.env` file and replace `you@gmail.com` with your Gmail address and `replace-locally` on the `GEMINI_API_KEY` line with a key from [Google AI Studio](https://aistudio.google.com/app/apikey). If `.env` is missing in a fresh copy, copy `.env.example` to `.env` and run `chmod 600 .env`. The default model is `gemini-3.5-flash`; check [current model pricing](https://ai.google.dev/gemini-api/docs/pricing) and your project's quota before syncing. Free-tier Gemini data may be used to improve Google's products.
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

The first sync with Shopee and TikTok Shop searches the full matching Gmail history, even if an older Shopee-only checkpoint exists. It spaces Gmail requests to stay within the per-user quota and retries temporary rate-limit errors. After a complete scan, the app saves one scan start time in SQLite for both shops. Later syncs search only mail received since that time, with a 60-second overlap, and retry any emails that previously failed to parse. Each processed email is saved immediately, so an interrupted sync can resume without parsing it again. Changing the Gmail account, sender settings, or either search query triggers another full scan. Refresh the dashboard after sync completes.

On first authorization, choose the same Google account as `GMAIL_ACCOUNT`. The app checks the authorized account before syncing. OAuth tokens are stored in `.secrets/token.json` with owner-only file permissions. Set `.secrets/credentials.json` to owner-only permissions too (`chmod 600 .secrets/credentials.json`). `.env`, `.secrets/`, and `data/` are Git ignored. Never commit any of them.

**Gmail scope:** `gmail.readonly` can read all messages and settings in the authorized mailbox. Google does not limit it to shopping mail. The app searches Shopee and TikTok Shop order, payment, refund, and cancellation subjects, checks the sender, and processes only matching messages. TikTok Shop defaults to the exact sender `no-reply@shop.tiktok.com`; change `TIKTOK_SENDER_EMAIL` and `TIKTOK_QUERY` together if your actual receipts use a different address. Shopee uses `SHOPEE_SENDER_DOMAIN` and `SHOPEE_QUERY`. Delivery and shipping notices are excluded by the default searches.

**Testing-mode OAuth:** Google may expire refresh tokens after seven days for an External app in Testing, so you may need to authorize again. Remove `.secrets/token.json` and run `python -m tracker auth` if the saved token no longer works. See [Google's OAuth audience guidance](https://support.google.com/cloud/answer/15549945).

## How accounting works

- Each order ID is unique within its shop in SQLite. A re-run skips processed Gmail IDs; later order events update the same order. Older emails cannot overwrite newer state.
- The per-order paid amount is the expense source of truth; each order ID is counted once.
- Cancelled and fully refunded orders count as zero. Partial refunds reduce the order's **Total**, and the recorded refund appears separately in **Refunds**. Shopee's **Return/Refund under review** notice is pending and does not count as a refund; the amount comes from **Review result for request** when it approves a refund. That email names a refund request, not the original purchase order. Approved refunds without an order link appear separately under **Refunds** and **Needs review**; they are not deducted from net spending until linked to a purchase. For TikTok Shop, the refund summary's final total is used rather than the gross product refund line. Missing partial-refund amounts appear in **Needs review**.
- Shopee coins used are tracked separately from vouchers and other discounts. Product Protection Subtotal is added to the Shopee order calculation, while the dashboard continues to count only the actual amount paid. The order check uses items + shipping + product protection − discounts − coins used = paid. When a Shopee receipt omits the coin line but its stated subtotal, vouchers, shipping, and paid total match the saved order, a positive remaining gap is recorded as **Shopee coins (inferred)**. TikTok Shop credits belong in discounts.
- Item categories split each order's net paid amount in proportion to the listed item line prices. This keeps category totals equal to net spending, including shipping, discounts, and coins used. If item prices are unavailable, the amount is **Unallocated**.
- Shopee item arithmetic and TikTok Shop package summaries are checked against the paid amount. TikTok item prices may already include coupons, so its check uses subtotal + shipping + fees − all coupons instead of subtracting coupons from item prices again. Excessive refunds and missing order IDs are flagged or rejected. Failed emails can be retried on the next sync. Check **Needs review** before relying on totals. Positive item-to-paid gaps recorded as Shopee coins used are inferred from arithmetic unless confirmed by the receipt.

The app strips likely address, phone and email details from receipt text before the Gemini call. It does not keep the raw receipt, but redaction is heuristic. Review the code before using it with sensitive mail. Gmail and Gemini API calls require internet access. The free-tier model and quotas may change.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use synthetic fixtures and do not access Gmail or Gemini.

Reference documentation: [Gmail Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python), [Gmail message search and full retrieval](https://developers.google.com/workspace/gmail/api/guides/list-messages), [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output).
