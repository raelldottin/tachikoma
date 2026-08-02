# Tachikoma Agent Instructions

Read in this order:

1. AGENTS.md
2. The current supervisor-generated slice prompt
3. The current context bundle
4. README.template
5. run.py
6. Relevant files under sdk/
7. Relevant tests

Repository rules:

- Work on one queued slice only.
- Never edit automation/queue/slices.json from inside a slice.
- Never use or expose real authentication material.
- Tests must mock all Pixel Starships network traffic.
- Do not run the scheduled live-account workflow as validation.
- Preserve existing gameplay strategy unless the slice explicitly changes it.
- Update README.template before README.md.
- Add regression coverage for every corrected defect.
- Stop when work exceeds allowed paths or max_files_changed.