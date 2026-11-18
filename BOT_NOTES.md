# Bot notes

This repo is Metaculus's [bot template](https://github.com/Metaculus/metac-bot-template) with a different bot in it. The template's README still applies for the basic setup. This page covers what is different.

There are two ways to run the bot. Both are in this repo.

| | The Claude route | The API route |
|---|---|---|
| Who forecasts | A scheduled Claude session, with its own web research | A panel of AI models called through OpenRouter |
| Cost | No extra money. It uses the owner's Claude plan | About $0.15 to $1.50 per question in model fees |
| Needs | The `METACULUS_TOKEN` secret | `METACULUS_TOKEN` and `OPENROUTER_API_KEY` |
| Status | **In use** | Built and rehearsed, switched off |

# The Claude route

## How it works

1. **A Claude session starts on a schedule.** It asks for the list of open questions by pushing a small request file (`state/poll-request.json`).
2. **GitHub writes the list.** The workflow `claude_route.yaml` holds the Metaculus token. It saves the open questions the bot has not answered to `state/open-questions.json`. The list leaves out what other forecasters think, because the tournament forbids copying the community.
3. **The Claude session researches and forecasts.** It follows [FORECASTING.md](FORECASTING.md): read the question like a contract, find a base rate, search for recent evidence, then write one file per question in `forecasts/pending/`, with its reasoning. It checks each file against the question and pushes it as soon as it is written.
4. **GitHub checks and posts.** The same workflow reads each file, refuses anything malformed, applies the guard rails (yes/no forecasts stay between 2% and 98%, every multiple-choice option gets at least 1%, percentiles must make a valid distribution for the question) and posts the forecast with the reasoning as a comment. The comment opens with the numbers that were posted. If the number goes out and the comment fails, the next run sends the comment, never a second forecast.
5. **Every forecast leaves a record.** Posted forecasts move to `forecasts/posted/`. Refused ones move to `forecasts/failed/` with the reason.

No person is involved while it runs, and no AI model is called from GitHub. The workflow starts when the Claude session pushes, so nothing depends on GitHub's timer.

## Market Pulse

The bot also plays the [Market Pulse Challenge 26Q4](https://www.metaculus.com/tournament/market-pulse-26q4/), which asks about markets every two weeks: return gaps between pairs of assets, the 10-year Treasury yield, the high-yield spread and the peak of the VIX. Only the forecast standing at a question's scoring time counts there, so the bot forecasts each question more than once:

| Reason on the list | When |
|---|---|
| `new` | The first time the bot sees the question |
| `weekly refresh` | The standing forecast is more than seven days old |
| `final day` | The scoring time is within 26 hours and no forecast was made that late |
| `last call` | The scoring time is within 3 hours and no forecast was made that late |

The scoring time is the one Metaculus states for each question. When it states none, the earlier of the close and the moment the community forecast is revealed is used. `list_questions.py` puts the work that is due on the same hourly list as the bot tournaments (at most 12 urgent and 4 patient questions per list), and `post_forecasts.py` lets a forecast be replaced in this tournament only. Each record in `forecasts/posted/` keeps its earlier forecasts under `earlier`.

One rule to know: a person may enter Market Pulse once, as a bot or as a human forecaster, not both.

## The pieces

| File | What it does |
|---|---|
| `FORECASTING.md` | The playbook the Claude session follows |
| `list_questions.py` | Writes the list of open questions (runs in GitHub Actions) |
| `post_forecasts.py` | Checks and posts the waiting forecasts (runs in GitHub Actions) |
| `check_forecast_files.py` | Lets the Claude session check its files, and hold them against the question list, before pushing |
| `forecast_math.py` | The checks and the maths, covered by `tests/` |
| `.github/workflows/claude_route.yaml` | Runs the two scripts when the session pushes |

## Running it

- **See what it did:** `state/last-run.json` holds the last run's one-line report (the file's history has the earlier ones), and the `forecasts/posted/` folder is the bot's record. On Metaculus, switch to the bot account (Settings → My Forecasting Bots); the reasoning is in each question's Private comments tab.
- **The schedule:** a scheduled Claude task named "Farago bot: hourly forecasts" starts a session once an hour. Questions stay open for three hours, so each one is seen by two or three runs; the first run that reaches it forecasts it.
- **Pause:** pause that scheduled task. With no session pushing, nothing runs.
- **Try it by hand:** Actions → "Claude route - post forecasts and list questions" → Run workflow. It posts whatever is waiting and refreshes the list.
- **First live run (18 November 2026):** eight practice forecasts covering every question type were posted to the bot testing area, then an unattended session posted the bot's first two tournament forecasts in 23 minutes.
- **Limits:** one forecast per scored question, at most eight questions or 35 minutes per run, questions that would close within ten minutes are left alone. Conditional questions are not handled yet and are listed under `skipped`.

# The API route

Everything below describes the other way to run the bot: `main.py` calling paid models. It is switched off (its 20-minute schedule is commented out in `run_bot_on_tournament.yaml`). It needs an `OPENROUTER_API_KEY` secret with credit behind it.

## How the bot forecasts

For every open question it has not answered yet:

1. **Research.** One to three web-search passes run in parallel, each with a different job: the latest developments, the outside view and history (base rates, trends, constraints), and what markets and experts expect. If AskNews keys are set, its news archive is added as one more source.
2. **A panel of models.** Several models each write an independent forecast from the same research. If a model fails, its seat goes to another model on the panel, and then to the backup models.
3. **Guard rails.** Yes/no forecasts are kept between 2% and 98%. Every multiple-choice option gets at least 1%. Numeric answers are tidied, and an answer that cannot be turned into a valid distribution is dropped on its own instead of sinking the question.
4. **Combine.** The framework takes the median of the panel for yes/no and numeric questions and the mean for multiple choice.
5. **Post.** The forecast goes to Metaculus with a comment holding the research and every model's reasoning.

No person is involved while it runs. The code is in `main.py` (talks to models and Metaculus) and `forecast_math.py` (settings and maths, covered by `tests/`).

## Tiers and rough cost

| Tier | Panel | Research passes | Rough cost per question |
|---|---|---|---|
| `lite` | Claude Opus 5.5 ×3 | news | about $0.15 |
| `standard` | Claude Opus 5.5 ×3, Gemini 4 Argon (high reasoning) | news, background | about $0.35 |
| `max` | Claude Opus 5.5 (high reasoning) ×2, Claude Opus 5.5, Gemini 4 Argon (high reasoning), GPT-6 Sol (high reasoning) | news, background, signals | about $1.50 |

The seasonal tournament runs on `standard` and MiniBench on `lite` unless you change them.

These are estimates, not measurements. They come from OpenRouter's list prices in November 2026 (Claude Opus 5.5 at $4 per million input tokens and $20 per million output tokens) and from the per-question costs Metaculus lists for its own high-reasoning bots. Gemini 4 Argon's price is reported to double on December 1, 2026, and the figures already assume the higher price. What costs money is hidden reasoning, which is billed as output: a plain Opus forecast is a few cents, and the same forecast at high reasoning effort is roughly ten times that.

The first days of real runs replace these figures. Every run prints its tracked spend, and the OpenRouter dashboard has the true number.

Metaculus says a season has 300 to 500 questions, and recent seasons have been near the low end. Each two-week MiniBench round is about 60 more. At the default settings, a season of about 330 questions plus its MiniBench rounds comes to roughly $190.

## Changing a setting

There are two ways, and neither touches the code:

- Edit `bot_settings.txt` in this repo and commit. This is the simple way.
- Add a repository variable with the same name (Settings → Secrets and variables → Actions → Variables). A variable wins over the file.

All settings are optional.

| Setting | Default | What it does |
|---|---|---|
| `BOT_TIER` | `standard` | Panel and research for the seasonal tournament: `lite`, `standard` or `max` |
| `BOT_MINIBENCH_TIER` | `lite` | Same for MiniBench, or `off` to skip MiniBench |
| `BOT_MAX_USD_PER_RUN` | `6` | Stop taking new questions in a run past this tracked spend (`0` = no cap) |
| `BOT_MAX_QUESTIONS_PER_RUN` | `20` | Most questions one run will start |
| `BOT_DRY_RUN` | off | `true` = forecast but post nothing |
| `BOT_FORECASTERS` | from tier | Your own panel, e.g. `opus,opus,sol` or full ids like `openrouter/x-ai/grok-4.7` |
| `BOT_MINIBENCH_FORECASTERS` | from tier | Same for MiniBench |
| `BOT_RESEARCH`, `BOT_MINIBENCH_RESEARCH` | from tier | Research passes: any of `news,background,signals` |
| `BOT_BACKUP_MODELS` | `opus,argon` | Models tried only when the whole panel has failed, or `none` |
| `BOT_PROB_FLOOR` | `0.02` | Yes/no forecasts stay inside this and one minus this |
| `BOT_OPTION_FLOOR` | `0.01` | Smallest probability for any multiple-choice option |
| `BOT_SEARCH_MODEL` | `sonar-pro` | The model that does the web research |
| `BOT_PARSER_MODEL` | `haiku` | The small model that reads the final answer out of each forecast |
| `BOT_BATCH_SIZE` | `5` | Questions forecast at the same time |
| `BOT_MAX_RUN_MINUTES` | `40` | A run stops taking new questions after this long |

Model short names: `opus`, `opus-high`, `argon`, `sol`, `haiku`, `sonar-pro`, `sonar-reasoning-pro`.

Keys and tokens never go in `bot_settings.txt`. The repo is public. The bot refuses to run if it finds anything in that file other than the settings above.

## Keeping spend bounded

The in-code cap is a safety net, and its cost tracking can undercount for new models. The hard stop is on OpenRouter's side: give the API key a credit limit, and the key stops working when the limit is reached.

A question the models keep failing on (for example, one they all judge already decided) is retried only a few times a day once it is more than a few hours old, so it cannot drain the budget.

## Running it

- **Smoke test:** Actions → Test Bot → Run workflow. It forecasts six questions from the [bot testing area](https://www.metaculus.com/tournament/bot-testing-area/), one of each question type first, which costs roughly $2 to $3 on the default tier. Tick "dry run" to post nothing.
- **Live:** the "Forecast on new AI tournament questions" workflow runs every 20 minutes. Tournament questions stay open for roughly 90 minutes.
- **Pause:** Actions → that workflow → ⋯ → Disable workflow.
- **See the forecasts:** on Metaculus, switch to the bot account (Settings → My Forecasting Bots). The reasoning is in each question's Private comments tab.
- **See how resolved forecasts scored:** Actions → Review recent forecasts → Run workflow. This is the template's own review tool and it spends nothing on models.
- **Local tests:** `uv run --with pytest python -m pytest tests -q`

A run goes red in GitHub when Metaculus refuses the token, or when every question fails with a key or credit error. One awkward question failing on its own does not.

## Things to remember

- The tournament rules bar a human in the loop. That includes changing the bot because of how it answered a question that is still open. Fix bugs and tune between questions, using resolved ones.
- Prize eligibility needs the participation form and the bot survey each season, and a description of how the bot works. This page is that description.
- GitHub pauses scheduled workflows in a public repo after 60 days without repository activity. A commit or a manual re-enable restarts them.
- The Metaculus Cup workflow is manual only here. The bot is built for the bot tournaments, so it does not spend money on the Cup on a schedule.
