---
name: jeeves-redline
description: Use this skill when the user asks to redline a document, compare two versions of a contract, add comments to a legal doc, review terms, or negotiate contract language. Also triggers for "track changes", "markup", or "show differences between these two docs."
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Document Redlining

Compare two Word documents and produce a redline, or add negotiation comments to contracts.

## Compare Two Documents

Produces a redlined Word doc with red strikethrough for deletions and blue underline for additions.

```bash
python /mnt/skills/custom/jeeves-redline/redline_tool.py compare "<file1>" "<file2>"
```

- `<file1>` and `<file2>` can be local paths, Google Drive file IDs, or Google Drive URLs
- Output saved to `$OUTPUTS_PATH/redline_output.docx` by default
- Use `--output <path>` to specify a custom output path

### Workflow

1. Fetch both documents from Google Drive (if URLs/IDs provided):
   ```bash
   python /mnt/skills/custom/google-drive/fetch_doc.py "<DOC1_URL>"
   python /mnt/skills/custom/google-drive/fetch_doc.py "<DOC2_URL>"
   ```
   Note: For .docx files, `fetch_doc.py` extracts text only. For redlining, the tool downloads the .docx files directly from Drive.

2. Run the comparison:
   ```bash
   python /mnt/skills/custom/jeeves-redline/redline_tool.py compare "<DRIVE_ID_1>" "<DRIVE_ID_2>"
   ```

3. Upload the result to Google Drive:
   ```bash
   python /mnt/skills/custom/google-drive/upload_to_drive.py "$OUTPUTS_PATH/redline_output.docx"
   ```

4. Share the Drive link with the user.

## Add Negotiation Comments

Reads a contract and adds comments at specific paragraphs.

```bash
python /mnt/skills/custom/jeeves-redline/redline_tool.py comment "<file>" "<comments.json>"
```

### Comments JSON format

Create a JSON file with an array of comment objects:

```json
[
  {
    "paragraph_match": "text snippet from the paragraph to comment on",
    "comment": "Your negotiation note here",
    "author": "Jeeves"
  }
]
```

### Workflow

1. Fetch the document and read its content to understand the terms
2. Formulate negotiation comments based on the user's instructions
3. Write comments JSON to a temp file
4. Run the comment tool
5. Upload the result to Google Drive and share the link

## Rules

- **Always upload the output to Google Drive** — the user cannot access local files
- State which version is "base" (first argument) vs "revised" (second argument) in comparisons
- Preserve original formatting where possible
- For large contracts, summarize the key changes found in the redline
- Never modify the original documents — always produce a new output file
