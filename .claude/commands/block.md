---
description: Make the app read or draw a TIA export it cannot handle yet
argument-hint: [path-to-export.xml] [what is wrong]
---

An export the app does not handle properly: $ARGUMENTS

These come from **Setup → Check all blocks** in the app, which lists every export it cannot fully draw.

Work in this order:

1. Look at the real XML first — never guess the format. `grep` for the part names, pin names and
   element names involved, and read the surrounding elements in full.
2. Compare what you see with section 5 of `CLAUDE.md` ("What we learned about the formats").
   If the file contradicts those notes, the file is right: fix the notes in the same change.
3. Find where the app gives up: `parse_doc` (block not understood), `parse_flg` (parts/wires),
   `chain_of` / `term_chains` (logic shape), `draw_net` / `net_svg` / `net_svg_fbd` (drawing),
   or `parse_graph_sequence` / `graph_svg` for S7-GRAPH.
4. Make the smallest change that handles this case, and keep every other language working.
5. Check it: `python tests/smoke.py`, then
   `python PLCDiff.pyw --files <old.xml> <new.xml> --out page.html --no-open` on the real export
   and an edited copy of it, and look at the page — a screenshot, not a guess.
6. Add what you learned about the format to section 5 of `CLAUDE.md`, and the new case to
   section 8 if it needs a new sample.
