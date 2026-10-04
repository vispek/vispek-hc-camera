# Prompt given to the agent

Verbatim, except that local paths are shortened to `<repo>` and `<scratch>`, and one rule
that named internal folders the agent must not open is left out.

---

You are testing whether a document is good enough to work from. Follow these rules
exactly; the test is void if you break them.

RULES
- You may read exactly ONE file: `<repo>/AGENTS.md`. Read it once, in full.
- Do NOT read, list, search or open anything else: no other file in that repository (no
  source code, no tests, no examples, no other docs even if AGENTS.md links to them),
  nothing elsewhere on the filesystem, no web. Do not run `ls`, `find`, `grep`, `cat`,
  `python -c "import ...; help(...)"`, `pip show`, or any command that inspects the
  package.
- Do NOT execute any Python or the `vispek-hc` command. You write the script; somebody
  else runs it.

TASK
Using only what AGENTS.md tells you, write a Python script that, with the `vispek_hc`
package:
1. opens the device (it must accept a `--simulate` command-line flag that uses the
   simulated device instead of hardware, and an `-o/--output` flag for the PNG path,
   default `frame.png`);
2. switches ONE visible LED on at a low brightness (choose a sensible LED and PWM from
   what the document says; it must not be an ultraviolet LED);
3. takes one camera frame while that LED is lit;
4. switches the LED off again, in a way that also holds if step 3 fails;
5. saves the frame as a PNG and prints one line saying which LED, which PWM, the
   picture's width and height, and the output path.

Write the script to exactly this path: `<scratch>/led_and_frame.py`

REPORT (as your final answer, plain text)
1. The full script you wrote.
2. For every `vispek_hc` name, argument or behaviour you used: the sentence or code line
   in AGENTS.md you took it from. Mark anything you had to GUESS because AGENTS.md did
   not say (be honest; guesses are the useful finding here).
3. What you wanted to know and could not find in AGENTS.md (missing or unclear things),
   as a short list.
4. Confirm which files you read (should be only AGENTS.md).
