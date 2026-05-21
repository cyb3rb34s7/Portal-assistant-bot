---
name: evidence-first-coding
description: Use before writing or editing code that touches any existing identifier (function, class, attribute, import, file path). Forces verification of what's actually in the codebase BEFORE typing, instead of inventing signatures from training-data assumptions. Reduces the failure mode where the model writes code calling non-existent functions, importing wrong modules, or using methods with hallucinated signatures, then wastes the operator's time finding out the code is broken.
---

# Evidence-First Coding

## The failure this prevents

A common failure: writing code based on what an API *should* look like
instead of what it *is*. Concrete examples seen in the wild:

- Importing `from foo import bar` when `bar` doesn't exist
- Calling `db.fetch(id, options)` when the real signature is
  `db.fetch(id, where=...)`
- Accessing `user.email_address` when the field is `user.email`
- Writing `@asynccontextmanager` because it's "the pattern" when
  the codebase uses a different idiom
- Using an API surface from a newer or older version of a library
  than what's actually installed

Each of these passes a quick eye-check, fails at runtime or in
review, and wastes time the operator could have spent on real work.

## When this skill fires

Trigger BEFORE the first `Write` or `Edit` tool call in a coding
task, specifically when:

- The user asked you to implement, add, modify, fix, or refactor
  something
- The work involves referencing existing code (calling a function,
  importing a module, modifying a file, using a library)
- ANY identifier — function name, class name, field name, attribute,
  import path, file path — is about to appear in your code that
  wasn't dictated character-for-character by the user

Do NOT trigger for:

- Pure new-file creation with no references to existing code
  (e.g. a fresh standalone script)
- Edits the user dictated exactly ("rename X to Y on line 47")
- Documentation-only changes (markdown, comments)

## The verification checklist

Before writing or editing, prove the following with tool calls.
If you cannot prove one, **STOP** and ask the user.

### 1. Every file you intend to modify, you actually read

For every file you'll touch:
- Call `Read` on the file
- Identify the line ranges you'll modify
- State in user-visible text: "I'm about to modify
  `path/to/file.py` lines N-M to add X. Current contents at those
  lines: <quote 3-5 lines>."

This single step catches "I'm modifying a file I never looked at"
which is the most expensive bug class.

### 2. Every identifier you reference exists with the signature you assume

For every `bar(x, y)` or `Foo.method()` or `from baz import qux`
that appears in code you're about to write:

- **If from this repo**: `Grep` for `def bar(` / `class Foo(` /
  the import path. Confirm the signature matches your call.
- **If from a third-party library**: read the library's docs OR
  find an existing use in this repo via `Grep` and copy that
  pattern verbatim. Do not infer from training-data memory of how
  the library "usually" looks.
- **If neither**: STOP. Don't fill the gap with a plausible guess.
  Ask the user.

### 3. Every "standard pattern" you'd copy has a precedent in this repo

Before writing something like `return ResponseEnvelope(...)`,
`@retry(...)`, `with span(...)`, or any "common idiom":

- `Grep` for at least one existing use of the pattern in this repo
- If none exists, the pattern may not match the project's
  conventions. Either ask the user OR use a simpler approach that
  matches what the codebase already does.

This catches "wrong-codebase-conventions" — code that looks right
in isolation but doesn't match how the project actually does things.

### 4. Every behavior you assume is verifiable

If you find yourself about to write something that depends on:

- "this function always returns a dict"
- "the response will have a `.data` field"
- "this runs on the main thread"
- "this is called only once at startup"

...stop. Either read the source to confirm, OR run a quick test,
OR explicitly downgrade your assumption to a defensive check.

## What to do when evidence is missing

When you can't verify something on the checklist:

1. **STOP writing.** Do not fill the gap with a plausible-looking
   guess. The guess is exactly what wastes the operator's time.
2. **Name what you don't know** in user-visible text. Specifically:
   "I'm not sure whether `SkillRunner.run()` accepts `overrides=`
   or `sub_step_overrides=`. Want me to grep, or do you know?"
3. **Wait for the operator's response** OR run the verifying tool
   call yourself.

## Anti-patterns

- "I'll add a `# TODO: verify` comment and move on" — no. Either
  verify now or stop.
- "This is the standard pattern in Django/React/etc." — fine, but
  show where THIS codebase uses it. If it doesn't, don't assume.
- "The function probably exists, let me try" — no. A `Grep` is
  ~100ms. The review round-trip on broken code is minutes plus
  the operator's attention.
- "I'll write it as I think it should be and we can fix it
  together" — that's exactly the waste this skill exists to
  prevent.
- "I'm confident in this because the user said do X" — the user
  said WHAT to do, not HOW. Verify the HOW.

## Why this is worth the friction

A `Grep` or `Read` tool call takes ~100ms. A code review round-trip
on broken code takes minutes plus the operator's full attention.
Two or three extra verification calls per coding task are nearly
free; the alternative is consistently wasting human time finding
out the code calls nonexistent functions.

This skill doesn't make the model smarter. It forces the model to
**look BEFORE assuming**, at the exact friction point where
assumptions get expensive.

## A worked example of doing it right

User: "Add a `cancel()` method to the Task class."

Wrong (assumes):
```
Looking at the Task class, I'll add cancel().
[writes code with `self._cancelled = True` and `if self._cancelled:
 raise CancelledError`]
```

Right (verifies):
```
I'll add cancel() to Task. Let me first read the existing Task class.
[calls Read on the file containing Task]

Task is defined at lib/task.py:23-95. It already has a private
`_cancelled` field set in __init__ (line 35), plus a property
`is_cancelled` that reads it (line 51). The existing pattern for
state transitions uses `self._lock` (visible at line 60).

So cancel() should:
  - acquire self._lock
  - set self._cancelled = True
  - call any cleanup hook if present

Let me grep for "cleanup_hook" or similar to confirm what cleanup
pattern this codebase uses.
[calls Grep for "cleanup"]

No cleanup hooks; the codebase relies on context-managers for
resource cleanup. So cancel() is just lock + flag.

[writes the actual code]
```

The "right" version takes ~5 extra seconds. The "wrong" version
takes ~5 extra minutes when the operator discovers it.

## Brief check before each Write/Edit

Internally, before calling Write or Edit, walk through this:

1. ✓ Read the target file?
2. ✓ Every existing identifier I'm using — verified?
3. ✓ Every "standard pattern" — has precedent here?
4. ✓ Every behavior I'm assuming — verifiable?

If any is ✗, stop and verify or ask.
