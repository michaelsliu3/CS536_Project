---
name: server-run-console
description: "Show the user a live console of every job run on the CS536 cloud GPU server. Use whenever you start, resume or restart anything on the server (vLLM, reviews, judging, labelling, a build, a download, a script in /root/rehearsal): run it in a screen session that writes a log, then open a Terminal panel tab that tails that log over SSH, without being asked."
---

# Live console for server runs (CS536_Project)

The repository owner wants to watch every server job as it runs. Each time you start
something on the cloud GPU server, they should get a console with its log without asking
for it.

## When

Every time you start, resume or restart a job on the server: the vLLM model server, a
review, judge or labelling run, a dataset build, a download, or any script in
`/root/rehearsal/`. Quick read-only checks (`nvidia-smi`, `git status`, a dry run) need no
console.

## How

1. **Use the SSH command the user gave last.** The provider changes the port when an
   instance is restarted or replaced, so an old port or the `cs536-cloud-westd` alias can be
   stale. Key authentication normally works; if it asks for a password, stop and let the
   user type it. Never type a password yourself.
2. **Run the job in a `screen` session that writes a log**, so it survives the SSH
   connection and the console can be reopened:
   ```bash
   screen -dmS <name> bash -lc "<command> 2>&1 | tee /root/rehearsal/<name>.log; exec bash -l"
   ```
   For a series of steps, put them in a script in `/root/rehearsal/` that appends to one log.
   Scratch scripts and logs there are the only files created on the server directly; code
   reaches the server only through git.
3. **Open the console right after the job starts**, in the user's Terminal panel
   (`mcp__terminal__run_in_terminal`), one tab per log:
   ```bash
   ssh -t -o ServerAliveInterval=30 -p <port> root@<host> 'tail -n 40 -F /root/rehearsal/<name>.log'
   ```
   Give the tab a title that starts with `server:` (for example `server: vLLM log`). Pass
   the project root as `cwd`.
4. **Check that it shows output** with `read_terminal`, then link the tab in your reply as
   `[its title](#terminal/<tab_id>)`, using the tab id the tool returned.
5. **Keep your own monitoring separate.** Wait for the job in your own background command
   (for example an SSH loop that greps the log for a final line), not by reading the user's
   tab. Leave the user's tab running when the job ends; close it only when the user asks.

## Starting vLLM

Start the 27B with the command in the serve notes of `notebooks/outcome_table.ipynb`, after
`source /root/vllm-env/bin/activate`, in `screen vllm`, with its output teed to a log in
`/root/rehearsal/`. Open a console on that log too, so the user can see it load (about
two to three minutes). `/root/rehearsal/serve.sh` serves only the small rehearsal model.

## Pitfalls

- `pgrep -f "vllm serve"` inside an SSH command also matches the SSH shell's own command
  line. Check `nvidia-smi` or `curl localhost:8000/v1/models` instead.
- `tail -F` keeps following a log that a resumed run recreates; `tail -f` does not.
- GitHub is reachable from the server only after `source /etc/network_turbo`, and
  `git -c http.version=HTTP/1.1 fetch` may need a few tries.
