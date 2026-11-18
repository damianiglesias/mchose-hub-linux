# Strict Execution Rules
- NEVER write or edit large files in a single tool call. Keep every `write` or `edit` call under 80 lines of code/text.
- If you need to create a large documentation or code file, create a short skeleton first with `write`, and then add sections incrementally using small `edit` calls.
- Keep all `bash` commands short (under 5 lines). Never dump large files or archives to stdout.
- Keep responses concise and stop immediately after executing a tool call.
