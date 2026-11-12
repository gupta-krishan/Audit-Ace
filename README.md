# Audit Ace 

AI-assisted GST accounting automation (India): tax invoices, credit/debit notes, e-way bill JSON and a sales register.

**Design rule:** the AI only *reads* text into structured data. All tax maths, validation, numbering and Section 34 checks are deterministic code, and a human approves every document.

## Privacy
- GSTIN, PAN, phone, email, IFSC and account numbers are masked before any API call.
- Ledger and client data stay local (`ledger.db`, `masters.json`, `out/` are git-ignored).
- Every approval is written to a hash-chained audit log.
- Never commit API keys or real client data. Keep this repo private.

## Run without installing anything (GitHub Codespaces)
1. Click the green **Code** button, then **Codespaces**, then **Create codespace on main**.
2. In the terminal that opens:
   ```
   python agent.py new --json samples/invoice.json
   python agent.py register
   ```
3. For the AI text input, add your key: `export ANTHROPIC_API_KEY=your_key`, then
   `python agent.py new "Invoice to Sharma Enterprises, 10 chairs at 2500 each, HSN 9401"`

## See a demo with zero setup
Go to the **Actions** tab, choose **Demo run**, click **Run workflow**. Open the finished run and download the `sample-output` artifact to see the generated invoice, credit note and e-way bill JSON.

## Run locally (if you install Python 3.10+)
```
pip install -r requirements.txt
cp masters.example.json masters.json   # then edit with your real details
python agent.py new --json samples/invoice.json
```

## Commands
- `new "text"` or `new --json file.json` creates a document (asks for approval; `--yes` skips the prompt)
- `register` shows net sales by GST rate (credit notes subtracted)

## Verify before relying on it
GST rate table, e-way bill JSON format, place-of-supply rules and Section 34 timelines change. Validate against the official portals and your professional judgement. Not tax advice.

## Roadmap
Purchase-bill ingestion, GSTR-2B reconciliation, e-invoice (IRN) and live e-way bill via a GSP API, Tally/Zoho export, web UI with multi-tenant encryption.
