---
name: jeeves-redline
description: Use this skill when the user asks to redline a document, compare two versions of a contract, add comments to a legal doc, review terms, read track changes, or negotiate contract language. Also triggers for "track changes", "markup", "suggestions", "show differences between these two docs", "what changed in this doc", "accept/reject changes", "strike", "push back", "counter", "redline this".
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Document Redlining — Compare, Track Changes, Negotiate

## Decision Tree — Which Command to Use

**Step 1: What is the user asking?**

| User says... | They want... | Use... |
|---|---|---|
| "Compare these two docs" / "What changed between v1 and v2?" / "Show me the differences" | See what's different between two document versions | `compare --track-changes` |
| "What did they change?" / "Review this redline" / "Read the tracked changes" | Understand edits someone else already made (doc has existing markup) | `read-changes` |
| "Strike 15% and put 10%" / "Change the termination to 30 days" / "Push back on this clause" / "Redline this with my edits" | Make specific text edits as tracked changes (legal negotiation style) | `suggest` |
| "Add a comment about the indemnity clause" / "Leave a note on section 3" | Add margin comments without changing text | `drive-comment` (for Google Docs) or `comment` (for .docx files) |

**Step 2: Key distinction — `compare` vs `suggest`**

- **`compare`** = You have TWO documents and want to see what's different. The tool **accepts all tracked changes in both docs first**, then compares the clean/net text. This means you can compare two "commented" versions and see only what actually changed between them — no need to find a clean version. Works with tables (term sheets, contracts).
- **`suggest`** = You have ONE document and the user tells you what to change. YOU build the specific find/replace pairs and the tool applies them as tracked changes.

---

## Command Reference

### 1. Compare Two Documents

**Always use `--track-changes`** — this produces proper Word revision markup (accept/reject in Review pane). The visual-only mode is only for when the user explicitly asks for a "visual redline" or "red strikethrough" format.

```bash
python /mnt/skills/custom/jeeves-redline/redline_tool.py compare "<original>" "<revised>" --track-changes
```

- `<original>` = the base/earlier version
- `<revised>` = the newer/modified version
- Files: local paths, Google Drive IDs, or Drive URLs
- Output: `$OUTPUTS_PATH/redline_output.docx`

**After comparing:** Summarize the key changes for the user (what was added, deleted, modified).

### 2. Read Existing Track Changes

When someone sends a document that already has tracked changes in it:

```bash
python /mnt/skills/custom/jeeves-redline/redline_tool.py read-changes "<file>"
```

Returns all insertions, deletions, and comments with author and date. Also outputs structured JSON.

**Use this FIRST** when the user forwards a redlined doc and asks "what did they change?" or "review this". Then summarize the changes and ask what the user wants to do next (accept, reject, counter-propose).

### 3. Suggest Edits (Negotiation Mode)

**This is the legal negotiation tool.** It takes a document and applies YOUR edits as proper Word tracked changes — strikethrough for deletions, colored text for insertions — so the recipient can accept/reject each one individually in Word's Review pane.

```bash
python /mnt/skills/custom/jeeves-redline/redline_tool.py suggest "<file>" "<changes.json>"
```

#### Workflow

1. **Read the document first** — fetch from Drive and read it to understand the current terms
2. **Identify exact text to change** — match the EXACT text as it appears in the document
3. **Build the changes JSON** — write to a temp file:

```json
[
  {
    "find": "15",
    "replace": "10",
    "author": "Jeeves",
    "_note": "15% → 10% — just strike the digits that change"
  },
  {
    "find": "6",
    "replace": "3",
    "author": "Jeeves",
    "_note": "60 days → 30 days — only the 6 changes to 3"
  },
  {
    "find": "25",
    "replace": "10",
    "author": "Jeeves",
    "_note": "1.25x → 1.10x — only strike 25, insert 10"
  },
  {
    "find": "clause to delete entirely",
    "replace": "",
    "author": "Jeeves"
  }
]
```

4. **Run the suggest command**
5. **Upload to Drive and share the link**
6. **Summarize what you changed** — list each edit so the user can review before sending

#### Tips for good suggest edits

- **Be maximally surgical — match ONLY the characters that change.** If changing "60 days" to "30 days", strike just the `6` and insert `3` — the `0 days` stays untouched. If changing "1.25x" to "1.10x", strike `25` and insert `10`. Never strike a whole sentence to change one number. The reader should see exactly what flipped with minimal red/blue.
- The `find` text must be an **exact substring** of the document — copy it precisely
- If a short match like `6` is ambiguous (appears many times), add just enough context to be unique: `"find": "60 days"` not the full sentence
- For deletions, set `replace` to `""`
- For insertions (adding new text where none exists), find the text immediately before and include it with the addition in `replace`
- When the user says "strike X and put Y", that means: `find: "X", replace: "Y"`

### 4. Add Comments to a Google Doc (Preferred)

**Use this when the document is a Google Doc.** Adds comments visible in the sidebar directly on the original doc — no download/re-upload needed. The `anchor_text` is included as a quote so readers know which text each comment refers to. Note: Google's API does not support highlighting/anchoring comments to specific text (known limitation since 2016) — only the Docs UI can do that. Tell the user the comments are on the doc with quoted context, and they can manually re-anchor them to highlighted text in ~2 minutes if needed.

```bash
python /mnt/skills/custom/jeeves-redline/redline_tool.py drive-comment "<google_doc_id_or_url>" "<comments.json>"
```

```json
[
  {
    "anchor_text": "60-day notice period",
    "comment": "We'd like to discuss this provision — the 60-day notice period is too long for our operations."
  },
  {
    "anchor_text": "",
    "comment": "General note: this agreement needs review by legal before signing."
  }
]
```

- `anchor_text`: exact text in the doc to attach the comment to. Leave empty for a file-level comment.
- Comments appear in the Google Doc's comment sidebar immediately.

### 4b. Add Comments to a Word File (.docx)

Only use this for local .docx files (not Google Docs):

```bash
python /mnt/skills/custom/jeeves-redline/redline_tool.py comment "<file>" "<comments.json>"
```

```json
[
  {
    "paragraph_match": "text snippet from the paragraph to comment on",
    "comment": "We'd like to discuss this provision — the 60-day notice period is too long for our operations.",
    "author": "Jeeves"
  }
]
```

---

## Common Scenarios

### "Compare these two versions and tell me what changed"
1. `compare` with `--track-changes`
2. Upload to Drive, share link
3. Summarize the key differences

### "They sent me a redline — what did they change?"
1. `read-changes` on the doc
2. Summarize: what was added, deleted, and any comments
3. Ask if the user wants to accept, reject, or counter-propose

### "Push back on the rate — strike 15% and put 10%, and change the notice period to 30 days"
1. Read the document first to find the exact text
2. Build changes JSON with precise find/replace pairs
3. `suggest` to apply as tracked changes
4. Upload to Drive, share link
5. List each edit made

### "Review this contract and suggest improvements"
1. Read the document
2. Analyze terms and identify areas to negotiate
3. Build changes JSON with suggested edits + reasoning
4. `suggest` to apply
5. Also consider `comment` for advisory notes that don't change text
6. Upload to Drive, share link, summarize all suggestions with rationale

---

## Rules

- **Always upload the output to Google Drive** and share the link
- **Always use `--track-changes` on compare** unless the user explicitly asks for a visual/formatting-only redline
- **Always read the document before using `suggest`** — you need exact text matches
- **Never modify the original documents** — always produce a new output file
- State which version is "base" vs "revised" in comparisons
- For large contracts, summarize the key changes found
- When using `suggest`, list every edit you made so the user can verify before sending to the counterparty
