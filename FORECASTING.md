# Forecasting playbook (the Claude route)

This file is written for a Claude session that starts on a schedule and forecasts for this bot. No person is watching the run. Read the whole file, then follow it exactly.

The division of labour:

- **You (the Claude session)** research each open question and write one forecast file per question.
- **GitHub Actions** (the workflow `claude_route.yaml`) holds the Metaculus token. It writes the list of open questions for you and posts the forecast files you push. It calls no AI model.

## Rules that never bend

1. **No human in the loop.** Do not ask anyone anything. Decide and carry on. If you are blocked, stop and say why in your final line.
2. **Never use the Metaculus community prediction.** Do not open or search for the question's page on Metaculus, and do not use other forecasters' comments on it. The tournament forbids copying the community. Other evidence is fine, including prediction markets and published expert forecasts.
3. **Only forecast questions from `state/open-questions.json`**, and write one forecast for each.
4. **Write only two kinds of file:** `state/poll-request.json` and `forecasts/pending/q<question_id>.json`. Change nothing else in the repository.
5. **Never handle a key or token.** You do not need one.
6. **Do not improvise.** If the list never arrives or a push keeps failing, stop and report it. Do not look for another way in.

## One run, step by step

`REPO` below is the folder the repository was cloned into. Shell variables do not survive between commands, so type the actual folder, request id and mode into each command.

### 1. Start from the newest state

```bash
git -C REPO fetch -q --depth=50 origin main && git -C REPO checkout -q -B run FETCH_HEAD
```

### 2. Ask for a fresh question list

Make up a request id once: the UTC time plus four random digits, for example `20261118T050000Z-4821`. `MODE` is `tournament` unless you were told to use `test_questions`.

```bash
cd REPO && mkdir -p state \
  && printf '{"request_id": "REQUEST_ID", "mode": "MODE"}\n' > state/poll-request.json \
  && git add state/poll-request.json \
  && git commit -q -m "Ask for the open questions (REQUEST_ID)" \
  && git -c push.negotiate=true push -q origin HEAD:main
```

If the push is refused because `main` moved, run `git fetch -q --depth=50 origin main && git rebase -q FETCH_HEAD` and push once more.

### 3. Wait for the list

The workflow needs one to three minutes. Give this command a timeout of seven minutes:

```bash
cd REPO && for i in $(seq 1 24); do sleep 15; git fetch -q --depth=50 origin main; \
  if git show FETCH_HEAD:state/open-questions.json 2>/dev/null | grep -q '"request_id": "REQUEST_ID"'; then echo READY; break; fi; done; \
  git checkout -q -B run FETCH_HEAD
```

If it never prints `READY`, stop. Final line: "The question list did not arrive; GitHub Actions may be switched off or slow."

### 4. Pick the questions

Read `state/open-questions.json`. If `errors` is not empty, mention it in your final line. If `questions` is empty, you are done: "No open questions."

Take the questions that close soonest first, **at most 6 per run**. Stop starting new questions once the run is 30 minutes old; the next run takes the rest. In `test_questions` mode take at most 5, one of each question type before a second of any type.

### 5. Research and forecast each question

Follow "How to forecast" below. Then write the file `REPO/forecasts/pending/q<question_id>.json` in the format under "The forecast file".

### 6. Check, then push

```bash
python3 REPO/check_forecast_files.py
```

Fix anything it refuses. Then:

```bash
cd REPO && git add forecasts/pending && git commit -q -m "Forecasts for N question(s)" \
  && git -c push.negotiate=true push -q origin HEAD:main
```

If the push is refused because `main` moved, fetch, rebase onto `FETCH_HEAD` as in step 2, and push once more.

### 7. See what happened

Wait up to five minutes (timeout of six minutes), then look:

```bash
cd REPO && for i in $(seq 1 20); do sleep 15; git fetch -q --depth=50 origin main; \
  if ! git ls-tree --name-only FETCH_HEAD forecasts/pending/ | grep -q 'q[0-9]*\.json'; then break; fi; done; \
  git ls-tree --name-only FETCH_HEAD forecasts/posted/ forecasts/failed/ | tail -20
```

A file in `forecasts/posted/` was posted. A file in `forecasts/failed/` was refused, and its `error` field says why. Do not rewrite a refused forecast in the same run.

### 8. Final line

One line: how many forecasts were posted (with question ids), how many failed and why, and how many questions were left for the next run.

## How to forecast

Peer scoring punishes confident misses far more than it rewards confident hits. Aim to be calibrated, not bold.

1. **Read the question like a contract.** What exactly must happen, by when, according to which source? Read the resolution criteria and the fine print twice. Many misses come from answering a slightly different question.
2. **Check the clock.** Note today's date and the resolution date. Most questions resolve within days or weeks. The shorter the time left, the more the present state of the world decides the answer.
3. **Outside view first.** Find a reference class and its base rate before reading the news. How often do things like this happen in a window this long? For a number, start from its current value and how much it usually moves over the remaining time.
4. **Then the inside view.** Search for what has happened recently and what is scheduled before the deadline: votes, meetings, releases, court dates, games, data publications. Three to six searches per question is the budget. Prefer primary sources and check the date of everything you read.
5. **Ask whether it has effectively happened already.** If the event has occurred or become impossible, confirm it against the exact criteria with a reliable source before going near the extremes.
6. **Combine.** Start at the base rate and move toward the evidence. Move far only for evidence that is direct, recent and reliable. Absence of news usually means the status quo holds.
7. **Sanity check.** State the strongest reason you could be wrong. Check units, scale (thousands or millions, percent or fraction) and date formats. Ask whether you would take the other side of your own forecast at these odds.

By question type:

- **Yes/no.** Give a probability between 0 and 1. The posting step keeps it between 2% and 98%, so do not agonise over the last point near the ends.
- **Multiple choice.** Give every option a probability; they must add up to 1. Copy the option names exactly as the list gives them. Never give an option 0.
- **Numbers.** Give percentiles 5, 10, 25, 50, 75, 90 and 95. Values must rise with the percentile. Make the range wide: the truth should fall outside your 5 to 95 range one time in ten. If a bound is marked closed (`open_lower_bound` or `open_upper_bound` is false), every value must stay inside it. Use the unit the question states.
- **Dates.** The same seven percentiles, as dates like `2027-03-01`.

## The forecast file

One JSON object per question, saved as `forecasts/pending/q<question_id>.json`. Copy `question_id`, `post_id` and `type` from the question list.

Yes/no:

```json
{
  "question_id": 12345,
  "post_id": 67890,
  "type": "binary",
  "probability": 0.27,
  "reasoning": "..."
}
```

Multiple choice:

```json
{
  "question_id": 12345,
  "post_id": 67890,
  "type": "multiple_choice",
  "probabilities": {"Option one": 0.55, "Option two": 0.35, "Option three": 0.10},
  "reasoning": "..."
}
```

Number (`"type"` is `numeric` or `discrete`, as the list says):

```json
{
  "question_id": 12345,
  "post_id": 67890,
  "type": "numeric",
  "percentiles": {"5": 41.0, "10": 43.5, "25": 47.0, "50": 51.0, "75": 55.5, "90": 60.0, "95": 63.0},
  "reasoning": "..."
}
```

Date:

```json
{
  "question_id": 12345,
  "post_id": 67890,
  "type": "date",
  "percentiles": {"5": "2027-01-10", "10": "2027-01-20", "25": "2027-02-15", "50": "2027-03-20", "75": "2027-05-01", "90": "2027-07-01", "95": "2027-09-01"},
  "reasoning": "..."
}
```

### The reasoning

`reasoning` is posted with the forecast as the bot's comment, which the tournament requires. Write 150 to 350 words of plain Markdown:

- what the question asks and when it resolves,
- the base rate or starting point you used,
- the two or three pieces of recent evidence that moved you, each with its source and date,
- what would have to happen for the other outcome,
- the final number.

No preamble and no mention of this playbook. It must be at least 200 characters or the forecast is refused.
