"""
Forecasting bot for the Metaculus FutureEval bot tournament and MiniBench.

Built on Metaculus's official template (github.com/Metaculus/metac-bot-template)
and its forecasting-tools framework. What this bot adds to the template:

  * Several research passes per question, each from a different angle (latest
    developments, outside view and history, market and expert expectations),
    run in parallel.
  * A mixed panel of forecasting models instead of one model run five times.
    If a model's provider fails, that forecast falls back to another model.
  * Guard rails on every answer: capped probabilities, a floor under every
    multiple-choice option, and numeric distributions that are repaired or
    rejected one forecaster at a time, so a single garbled answer is dropped
    instead of sinking the whole question.
  * Spending controls: questions closing soonest go first, a cost cap per run,
    and rare retries for questions that keep failing.

Nothing here needs a person while it runs. The dials live in bot_settings.txt
(or in environment variables of the same names, which win) and are described in
BOT_NOTES.md.
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import dotenv

# Runtime helpers from the template (env validation, banners, quieter logs).
from bot_helpers import (
    check_environment,
    print_run_summary_banner,
    print_startup_banner,
    silence_noisy_dependencies,
)

silence_noisy_dependencies()

from forecasting_tools import (  # noqa: E402
    AskNewsSearcher,
    BinaryPrediction,
    BinaryQuestion,
    ConditionalPrediction,
    ConditionalQuestion,
    DatePercentile,
    DateQuestion,
    ForecastBot,
    ForecastReport,
    GeneralLlm,
    MetaculusClient,
    MetaculusQuestion,
    MonetaryCostManager,
    MultipleChoiceQuestion,
    NumericDistribution,
    NumericQuestion,
    Percentile,
    PredictedOption,
    PredictedOptionList,
    PredictionAffirmed,
    PredictionTypes,
    ReasonedPrediction,
    clean_indents,
    structure_output,
)

import forecast_math as fm  # noqa: E402

dotenv.load_dotenv()
logger = logging.getLogger(__name__)

RunMode = Literal["tournament", "metaculus_cup", "test_questions"]

# The bot's dials. Never keys or tokens: this file is public.
SETTINGS_FILE = Path(__file__).with_name("bot_settings.txt")

RESEARCH_TITLES = {
    "news": "Latest developments",
    "background": "Outside view and history",
    "signals": "Market and expert expectations",
    "asknews": "News articles",
}

RESEARCH_TASKS = {
    "news": """Your task: report the latest developments that bear on this question.
1. Current status: the most recent facts, each with its date. Say plainly if the outcome already looks decided under the resolution criteria, and quote what decides it.
2. Scheduled events, deadlines, votes, rulings or data releases that fall before the question resolves.
3. What the primary or official source says, especially the source named in the resolution criteria.
Prefer primary and recent sources, give a date for every claim, and link your sources. If you find nothing recent, say so.""",
    "background": """Your task: establish the outside view for this question.
1. Reference class and base rate: how often have comparable events happened over a comparable length of time? Give numbers and where they come from.
2. History of this specific case: earlier attempts, the trend so far, and for any quantity the question asks about, its latest value and its recent range.
3. Constraints that make the outcome more or less likely in the time left: legal, procedural, physical or scheduling.
Give dates and link your sources. Where no good base rate exists, say so and give the nearest comparison.""",
    "signals": """Your task: find what informed others expect.
Look for prediction markets (such as Polymarket, Kalshi and Manifold), bookmakers' odds, analyst or consensus forecasts, official projections and polls that bear on this question.
For each one give the number, the date it was observed and a link. Note anything that makes a figure hard to compare with this question, such as a different deadline or definition.
Leave Metaculus itself out. If you find no such figures, say so.""",
}

NO_RESEARCH_NOTE = (
    "No fresh research could be gathered for this question. Rely on what you "
    "already know, allow that things may have changed since, and be less "
    "confident than you otherwise would be."
)


class EnsembleForecaster(ForecastBot):
    """
    Researches each question from several angles, asks a panel of models for
    independent forecasts, and lets the framework combine them (median for yes/no
    and numeric questions, mean for multiple choice).

    Create instances with EnsembleForecaster.build(...).
    """

    _structure_output_validation_samples = 2
    _max_concurrent_searches = 4
    _research_char_limit = 14000

    # Filled in by build(). Declared here so the base class can call
    # make_llm_dict() during its own __init__.
    _plan: fm.TierPlan | None = None
    _settings: fm.BotSettings | None = None
    _seat_names: tuple[str, ...] = ()
    _forecaster_llms: dict[str, GeneralLlm] = {}
    _search_llm: GeneralLlm | None = None

    @classmethod
    def build(
        cls,
        plan: fm.TierPlan,
        settings: fm.BotSettings,
        *,
        skip_previously_forecasted: bool,
        metaculus_client: MetaculusClient | None = None,
    ) -> "EnsembleForecaster":
        # One model object per distinct name: the panel, then the backups that
        # are only used if every model on the panel fails.
        forecaster_llms = {
            name: GeneralLlm(**fm.resolve_model_spec(name))
            for name in dict.fromkeys((*plan.forecasters, *settings.backup_models))
        }
        search_llm = GeneralLlm(**fm.resolve_model_spec(settings.search_model))
        parser_llm = GeneralLlm(**fm.resolve_model_spec(settings.parser_model))

        bot = cls(
            research_reports_per_question=1,
            predictions_per_research_report=len(plan.forecasters),
            use_research_summary_to_forecast=False,
            publish_reports_to_metaculus=not settings.dry_run,
            folder_to_save_reports_to=None,
            skip_previously_forecasted_questions=skip_previously_forecasted,
            extra_metadata_in_explanation=True,
            # The summary is only decoration in the comment, so skip paying for it.
            enable_summarize_research=False,
            # One good forecast beats none when a question closes in 90 minutes.
            required_successful_predictions=0.3,
            llms={
                "default": forecaster_llms[plan.forecasters[0]],
                "summarizer": parser_llm,
                "researcher": search_llm,
                "parser": parser_llm,
            },
            metaculus_client=metaculus_client,
        )
        bot._plan = plan
        bot._settings = settings
        bot._seat_names = tuple(plan.forecasters)
        bot._forecaster_llms = forecaster_llms
        bot._search_llm = search_llm
        bot._semaphores = {}
        return bot

    def make_llm_dict(self) -> dict:
        llm_dict = super().make_llm_dict()
        llm_dict["forecaster_seats"] = ", ".join(self._seat_names)
        if self._plan is not None:
            llm_dict["research_passes"] = ", ".join(self._plan.research)
        if self._settings is not None:
            llm_dict["backup_models"] = ", ".join(self._settings.backup_models)
        return llm_dict

    ##################################### SMALL HELPERS #####################################

    def _semaphore(self, name: str, size: int) -> asyncio.Semaphore:
        # Created on first use so it belongs to the event loop that is running,
        # and kept on the instance so two bots never share one.
        if "_semaphores" not in self.__dict__:
            self._semaphores = {}
        if name not in self._semaphores:
            self._semaphores[name] = asyncio.Semaphore(size)
        return self._semaphores[name]

    async def _notes(self, question: MetaculusQuestion) -> dict:
        """Scratch space that lasts for one question (shared by its forecasts)."""
        try:
            notepad = await self._get_notepad(question)
        except ValueError:
            return {}
        return notepad.note_entries

    @staticmethod
    def _text(value: str | None) -> str:
        return value.strip() if value and value.strip() else "(none given)"

    @staticmethod
    def _optional_text(value: str | None) -> str:
        return value.strip() if value and value.strip() else ""

    @staticmethod
    def _timing(question: MetaculusQuestion) -> str:
        return fm.describe_timing(
            datetime.now(timezone.utc),
            question.close_time,
            question.scheduled_resolution_time,
        )

    @staticmethod
    def _label(model_name: str, reasoning: str) -> str:
        return f"*Forecaster model: {model_name}*\n\n{reasoning}"

    ##################################### RESEARCH #####################################

    async def run_research(self, question: MetaculusQuestion) -> str:
        assert self._plan is not None
        sources = list(self._plan.research)
        jobs = [self._search(source, question) for source in sources]
        if self._asknews_is_configured():
            sources.append("asknews")
            jobs.append(self._asknews(question))

        results = await asyncio.gather(*jobs, return_exceptions=True)

        sections: list[str] = []
        for source, result in zip(sources, results):
            if isinstance(result, BaseException):
                logger.warning(
                    f"Research pass '{source}' failed for {question.page_url}: "
                    f"{type(result).__name__}: {result}"
                )
                continue
            text = fm.demote_headings(str(result or "").strip())
            if text:
                text = fm.shorten(text, self._research_char_limit)
                sections.append(f"## {RESEARCH_TITLES[source]}\n{text}")

        if not sections:
            notes = await self._notes(question)
            notes["research_failed"] = True
            logger.warning(f"No research at all for {question.page_url}")
            return f"## Research\n{NO_RESEARCH_NOTE}"

        research = "\n\n".join(sections)
        logger.info(
            f"Research for {question.page_url}: {len(sections)} of {len(sources)} "
            f"passes succeeded, {len(research)} characters"
        )
        return research

    async def _search(self, source: str, question: MetaculusQuestion) -> str:
        assert self._search_llm is not None
        prompt = self._research_prompt(source, question)
        async with self._semaphore("search", self._max_concurrent_searches):
            return await self._search_llm.invoke(prompt)

    @staticmethod
    def _asknews_is_configured() -> bool:
        has_oauth = bool(os.getenv("ASKNEWS_CLIENT_ID") and os.getenv("ASKNEWS_SECRET"))
        return has_oauth or bool(os.getenv("ASKNEWS_API_KEY"))

    async def _asknews(self, question: MetaculusQuestion) -> str:
        # The free AskNews tier allows about one call every ten seconds.
        async with self._semaphore("asknews", 1):
            return await AskNewsSearcher().call_preconfigured_version(
                "asknews/news-summaries", question.question_text
            )

    def _research_prompt(self, source: str, question: MetaculusQuestion) -> str:
        answer_shape = ""
        if isinstance(question, MultipleChoiceQuestion):
            answer_shape = f"The possible answers are: {question.options}"
        elif isinstance(question, NumericQuestion):
            unit = question.unit_of_measure or "not stated"
            answer_shape = f"The answer is a number. Units: {unit}"
        elif isinstance(question, DateQuestion):
            answer_shape = "The answer is a date."

        header = clean_indents(
            f"""
            You are a research assistant to a professional forecaster. You find facts. You do not make forecasts or give probabilities of your own.

            The forecasting question:
            {question.question_text}
            {answer_shape}

            How the question resolves:
            {self._text(question.resolution_criteria)}

            {self._optional_text(question.fine_print)}

            Background supplied with the question:
            {self._text(question.background_info)}

            {self._timing(question)}
            """
        ).strip()
        return f"{header}\n\n{RESEARCH_TASKS[source]}"

    ##################################### THE PANEL #####################################

    async def _invoke_forecaster(
        self, question: MetaculusQuestion, prompt: str
    ) -> tuple[str, str]:
        """
        Gives the prompt to the next model on the panel and returns (answer, model
        name). If that model fails, the other panel models are tried in turn, and
        then the backups.
        """
        assert self._settings is not None
        notes = await self._notes(question)
        seat = notes.get("next_seat", 0)
        notes["next_seat"] = seat + 1

        last_error: Exception | None = None
        for name in fm.fallback_order(
            self._seat_names, seat, self._settings.backup_models
        ):
            try:
                reasoning = await self._forecaster_llms[name].invoke(prompt)
                return reasoning, name
            except Exception as error:
                last_error = error
                logger.warning(
                    f"Forecaster '{name}' failed for {question.page_url}: "
                    f"{type(error).__name__}: {str(error)[:300]}"
                )
        raise RuntimeError(
            f"Every forecaster failed for {question.page_url}. Last error: {last_error}"
        ) from last_error

    async def _probability_floor(self, question: MetaculusQuestion) -> float:
        assert self._settings is not None
        notes = await self._notes(question)
        if notes.get("research_failed"):
            return max(self._settings.prob_floor, 0.05)
        return self._settings.prob_floor

    ##################################### BINARY QUESTIONS #####################################

    async def _run_forecast_on_binary(
        self, question: BinaryQuestion, research: str
    ) -> ReasonedPrediction[float]:
        prompt = clean_indents(
            f"""
            You are a professional forecaster answering a question in a tournament that is scored on accuracy.

            The question:
            {question.question_text}

            Question background:
            {self._text(question.background_info)}

            This question's outcome will be determined by the specific criteria below. Treat them as not yet satisfied unless the research clearly shows otherwise:
            {self._text(question.resolution_criteria)}

            {self._optional_text(question.fine_print)}

            Your research assistants report the following. They can be wrong or out of date, so weigh each item by its source and its date:
            {research}

            {self._timing(question)}

            Before answering you write, briefly:
            (a) The time left until the outcome to the question is known, and what would have to happen in that time.
            (b) The outside view: a reference class for this kind of event and its base rate over a similar length of time.
            (c) The status quo outcome if nothing changed.
            (d) The strongest evidence for Yes and the strongest evidence for No, with dates.
            (e) A brief description of a scenario that results in a No outcome, and one that results in a Yes outcome.
            (f) A literal check of the resolution criteria and fine print: the exact source, thresholds, deadlines and edge cases.

            You write your rationale remembering that good forecasters put extra weight on the status quo outcome since the world changes slowly most of the time, and that they neither round unlikely outcomes down to zero nor likely ones up to certainty.
            If the research mentions a Metaculus community forecast for this question, set it aside and reason from the evidence.
            {self._get_conditional_disclaimer_if_necessary(question)}

            The last thing you write is your final answer as: "Probability: ZZ%", 0-100
            """
        )
        return await self._binary_prompt_to_forecast(question, prompt)

    async def _binary_prompt_to_forecast(
        self,
        question: BinaryQuestion,
        prompt: str,
    ) -> ReasonedPrediction[float]:
        reasoning, model_name = await self._invoke_forecaster(question, prompt)
        parsing_instructions = clean_indents(
            f"""
            The text given to you is trying to give a probability forecast for a binary question.
            {self._create_resolved_question_parsing_message()}
            """
        )
        binary_prediction: BinaryPrediction = await structure_output(
            reasoning,
            BinaryPrediction,
            model=self.get_llm("parser", "llm"),
            num_validation_samples=self._structure_output_validation_samples,
            additional_instructions=parsing_instructions,
        )
        floor = await self._probability_floor(question)
        decimal_pred = fm.clamp_probability(
            binary_prediction.prediction_in_decimal, floor
        )
        logger.info(
            f"{model_name} forecast {decimal_pred:.3f} for {question.page_url}"
        )
        return ReasonedPrediction(
            prediction_value=decimal_pred, reasoning=self._label(model_name, reasoning)
        )

    ##################################### MULTIPLE CHOICE QUESTIONS #####################################

    async def _run_forecast_on_multiple_choice(
        self, question: MultipleChoiceQuestion, research: str
    ) -> ReasonedPrediction[PredictedOptionList]:
        prompt = clean_indents(
            f"""
            You are a professional forecaster answering a question in a tournament that is scored on accuracy.

            The question:
            {question.question_text}

            The options are: {question.options}

            Background:
            {self._text(question.background_info)}

            {self._text(question.resolution_criteria)}

            {self._optional_text(question.fine_print)}

            Your research assistants report the following. They can be wrong or out of date, so weigh each item by its source and its date:
            {research}

            {self._timing(question)}

            Before answering you write, briefly:
            (a) The time left until the outcome to the question is known.
            (b) The outside view: how often each kind of outcome has happened in comparable cases.
            (c) The status quo outcome if nothing changed.
            (d) The strongest evidence that moves probability between the options, with dates.
            (e) A description of a scenario that results in an unexpected outcome.
            (f) A literal check of how the resolution criteria map outcomes onto the options, including boundary cases.

            {self._get_conditional_disclaimer_if_necessary(question)}
            You write your rationale remembering that (1) good forecasters put extra weight on the status quo outcome since the world changes slowly most of the time, and (2) good forecasters leave some moderate probability on most options to account for unexpected outcomes.
            If the research mentions a Metaculus community forecast for this question, set it aside and reason from the evidence.

            The last thing you write is your final probabilities for the N options in this order {question.options} as:
            Option_A: Probability_A
            Option_B: Probability_B
            ...
            Option_N: Probability_N
            """
        )
        return await self._multiple_choice_prompt_to_forecast(question, prompt)

    async def _multiple_choice_prompt_to_forecast(
        self,
        question: MultipleChoiceQuestion,
        prompt: str,
    ) -> ReasonedPrediction[PredictedOptionList]:
        assert self._settings is not None
        parsing_instructions = clean_indents(
            f"""
            Make sure that all option names are one of the following:
            {question.options}

            The text you are parsing may prepend these options with some variation of "Option" which you should remove if not part of the option names I just gave you.
            Additionally, you may sometimes need to parse a 0% probability. Please do not skip options with 0% but rather make it an entry in your final list with 0% probability.
            {self._create_resolved_question_parsing_message()}
            """
        )
        reasoning, model_name = await self._invoke_forecaster(question, prompt)
        parsed: PredictedOptionList = await structure_output(
            text_to_structure=reasoning,
            output_type=PredictedOptionList,
            model=self.get_llm("parser", "llm"),
            num_validation_samples=self._structure_output_validation_samples,
            additional_instructions=parsing_instructions,
        )

        # Line the answer up with the real option names, in the question's order,
        # and keep every option above a small floor. A mismatch raises here, so
        # only this forecaster's answer is dropped.
        probabilities = fm.align_options(
            [(option.option_name, option.probability) for option in parsed.predicted_options],
            question.options,
        )
        floor = fm.effective_option_floor(
            len(question.options), self._settings.option_floor
        )
        probabilities = fm.floor_and_normalize(probabilities, floor)
        predicted_option_list = PredictedOptionList(
            predicted_options=[
                PredictedOption(option_name=name, probability=probability)
                for name, probability in zip(question.options, probabilities)
            ]
        )

        logger.info(
            f"{model_name} forecast {predicted_option_list.to_dict()} "
            f"for {question.page_url}"
        )
        return ReasonedPrediction(
            prediction_value=predicted_option_list,
            reasoning=self._label(model_name, reasoning),
        )

    ##################################### NUMERIC QUESTIONS #####################################

    async def _run_forecast_on_numeric(
        self, question: NumericQuestion, research: str
    ) -> ReasonedPrediction[NumericDistribution]:
        upper_bound_message, lower_bound_message = (
            self._create_upper_and_lower_bound_messages(question)
        )
        prompt = clean_indents(
            f"""
            You are a professional forecaster answering a question in a tournament that is scored on accuracy.

            The question:
            {question.question_text}

            Background:
            {self._text(question.background_info)}

            {self._text(question.resolution_criteria)}

            {self._optional_text(question.fine_print)}

            Units for answer: {question.unit_of_measure if question.unit_of_measure else "Not stated (please infer this)"}

            Your research assistants report the following. They can be wrong or out of date, so weigh each item by its source and its date:
            {research}

            {self._timing(question)}

            {lower_bound_message}
            {upper_bound_message}

            Formatting Instructions:
            - Please notice the units requested and give your answer in these units (e.g. whether you represent a number as 1,000,000 or 1 million).
            - Never use scientific notation.
            - Always start with a smaller number (more negative if negative) and then increase from there. The value for percentile 10 should always be less than the value for percentile 20, and so on.

            Before answering you write, briefly:
            (a) The time left until the outcome to the question is known.
            (b) The latest known value and exactly what is being measured, by which source.
            (c) The outcome if nothing changed.
            (d) The outcome if the current trend continued.
            (e) How much this quantity has typically moved over a period this long.
            (f) The expectations of experts and markets.
            (g) A brief description of an unexpected scenario that results in a low outcome.
            (h) A brief description of an unexpected scenario that results in a high outcome.

            {self._get_conditional_disclaimer_if_necessary(question)}
            You remind yourself that good forecasters are humble and set wide 90/10 confidence intervals to account for unknown unknowns. Your range should be wide enough that the outcome landing outside it would surprise you only one time in five.
            If the research mentions a Metaculus community forecast for this question, set it aside and reason from the evidence.

            The last thing you write is your final answer as:
            "
            Percentile 10: XX (lowest number value)
            Percentile 20: XX
            Percentile 40: XX
            Percentile 60: XX
            Percentile 80: XX
            Percentile 90: XX (highest number value)
            "
            """
        )
        return await self._numeric_prompt_to_forecast(question, prompt)

    async def _numeric_prompt_to_forecast(
        self,
        question: NumericQuestion,
        prompt: str,
    ) -> ReasonedPrediction[NumericDistribution]:
        reasoning, model_name = await self._invoke_forecaster(question, prompt)
        parsing_instructions = clean_indents(
            f"""
            The text given to you is trying to give a forecast distribution for a numeric question.
            - This text is trying to answer the numeric question: "{question.question_text}".
            {self._create_single_distribution_parsing_message(question)}
            {self._create_resolved_question_parsing_message()}
            - When parsing the text, please make sure to give the values (the ones assigned to percentiles) in terms of the correct units.
            - The units for the forecast are: {question.unit_of_measure}
            - Your work will be shown publicly with these units stated verbatim after the numbers your parse.
            - As an example, someone else guessed that the answer will be between {question.lower_bound} {question.unit_of_measure} and {question.upper_bound} {question.unit_of_measure}, so the numbers parsed from an answer like this would be verbatim "{question.lower_bound}" and "{question.upper_bound}".
            - If the answer doesn't give the answer in the correct units, you should parse it in the right units. For instance if the answer gives numbers as $500,000,000 and units are "B $" then you should parse the answer as 0.5 (since $500,000,000 is $0.5 billion).
            - If percentiles are not explicitly given (e.g. only a single value is given) please don't return a parsed output, but rather indicate that the answer is not explicitly given in the text.
            - Turn any values that are in scientific notation into regular numbers.
            """
        )
        percentile_list: list[Percentile] = await structure_output(
            reasoning,
            list[Percentile],
            model=self.get_llm("parser", "llm"),
            additional_instructions=parsing_instructions,
            num_validation_samples=self._structure_output_validation_samples,
        )
        prediction = self._build_distribution(
            [(percentile.percentile, percentile.value) for percentile in percentile_list],
            question,
        )
        logger.info(
            f"{model_name} forecast {prediction.declared_percentiles} "
            f"for {question.page_url}"
        )
        return ReasonedPrediction(
            prediction_value=prediction, reasoning=self._label(model_name, reasoning)
        )

    @staticmethod
    def _build_distribution(
        pairs: list[tuple[float, float]],
        question: NumericQuestion | DateQuestion,
    ) -> NumericDistribution:
        """
        Tidies the percentiles and builds the distribution, then works out its
        full curve straight away. Anything invalid raises here, where it costs one
        forecaster's answer, instead of later when the answers are combined, where
        it would cost the whole question.
        """
        percentiles = [
            Percentile(percentile=percentile, value=value)
            for percentile, value in fm.clean_percentiles(pairs)
        ]
        prediction = NumericDistribution.from_question(percentiles, question)
        prediction.get_cdf()
        return prediction

    ##################################### DATE QUESTIONS #####################################

    async def _run_forecast_on_date(
        self, question: DateQuestion, research: str
    ) -> ReasonedPrediction[NumericDistribution]:
        upper_bound_message, lower_bound_message = (
            self._create_upper_and_lower_bound_messages(question)
        )
        prompt = clean_indents(
            f"""
            You are a professional forecaster answering a question in a tournament that is scored on accuracy.

            The question:
            {question.question_text}

            Background:
            {self._text(question.background_info)}

            {self._text(question.resolution_criteria)}

            {self._optional_text(question.fine_print)}

            Your research assistants report the following. They can be wrong or out of date, so weigh each item by its source and its date:
            {research}

            {self._timing(question)}

            {lower_bound_message}
            {upper_bound_message}

            Formatting Instructions:
            - This is a date question, and as such, the answer must be expressed in terms of dates.
            - The dates must be written in the format of YYYY-MM-DD. If hours matter, please append the date with the hour in UTC and military time: YYYY-MM-DDTHH:MM:SSZ. No other formatting is allowed.
            - Always start with a lower date chronologically and then increase from there.
            - Do NOT forget this. The dates must be written in chronological order starting at the earliest time at percentile 10 and increasing from there.

            Before answering you write, briefly:
            (a) The time left until the outcome to the question is known.
            (b) The outcome if nothing changed.
            (c) The outcome if the current trend continued.
            (d) How long comparable things have taken in the past.
            (e) The expectations of experts and markets.
            (f) A brief description of an unexpected scenario that results in an early outcome.
            (g) A brief description of an unexpected scenario that results in a late outcome.

            {self._get_conditional_disclaimer_if_necessary(question)}
            You remind yourself that good forecasters are humble and set wide 90/10 confidence intervals to account for unknown unknowns, and that things usually take longer than planned.
            If the research mentions a Metaculus community forecast for this question, set it aside and reason from the evidence.

            The last thing you write is your final answer as:
            "
            Percentile 10: YYYY-MM-DD (oldest date)
            Percentile 20: YYYY-MM-DD
            Percentile 40: YYYY-MM-DD
            Percentile 60: YYYY-MM-DD
            Percentile 80: YYYY-MM-DD
            Percentile 90: YYYY-MM-DD (newest date)
            "
            """
        )
        return await self._date_prompt_to_forecast(question, prompt)

    async def _date_prompt_to_forecast(
        self,
        question: DateQuestion,
        prompt: str,
    ) -> ReasonedPrediction[NumericDistribution]:
        reasoning, model_name = await self._invoke_forecaster(question, prompt)
        parsing_instructions = clean_indents(
            f"""
            The text given to you is trying to give a forecast distribution for a date question.
            - This text is trying to answer the question: "{question.question_text}".
            {self._create_single_distribution_parsing_message(question)}
            {self._create_resolved_question_parsing_message()}
            - As an example, someone else guessed that the answer will be between {question.lower_bound} and {question.upper_bound}, so the numbers parsed from an answer like this would be verbatim "{question.lower_bound}" and "{question.upper_bound}".
            - The output is given as dates/times please format it into a valid datetime parsable string. Assume midnight UTC if no hour is given.
            - If percentiles are not explicitly given (e.g. only a single value is given) please don't return a parsed output, but rather indicate that the answer is not explicitly given in the text.
            """
        )
        date_percentile_list: list[DatePercentile] = await structure_output(
            reasoning,
            list[DatePercentile],
            model=self.get_llm("parser", "llm"),
            additional_instructions=parsing_instructions,
            num_validation_samples=self._structure_output_validation_samples,
        )
        prediction = self._build_distribution(
            [
                (percentile.percentile, percentile.value.timestamp())
                for percentile in date_percentile_list
            ],
            question,
        )
        logger.info(
            f"{model_name} forecast {prediction.declared_percentiles} "
            f"for {question.page_url}"
        )
        return ReasonedPrediction(
            prediction_value=prediction, reasoning=self._label(model_name, reasoning)
        )

    ##################################### SHARED PARSING MESSAGES (from the template) #####################################

    def _create_resolved_question_parsing_message(self) -> str:
        return "- If the text concludes that the question has already resolved *in the past* (i.e. it treats the question as decided rather than something to forecast), please DO NOT return a parsed output, even if a final forecast is also given. Instead indicate that the answer is not explicitly given in the text.\n"

    def _create_single_distribution_parsing_message(
        self, question: NumericQuestion | DateQuestion
    ) -> str:
        message = (
            "- The text may contain multiple percentile distributions (e.g. forecasts for several related questions/entities, or intermediate drafts before a final answer). You must return exactly ONE distribution: the single final distribution that answers the question stated above.\n"
            "- Never merge or concatenate percentile lists that refer to different entities, options, or scenarios. Each percentile should appear at most once in your output.\n"
            "- If there are multiple final distributions and you cannot tell which one answers the question stated above, do not guess or combine them. Instead indicate that the answer is not explicitly given in the text."
        )
        if question.group_question_option is not None:
            message += (
                f'\n- This question is specifically about "{question.group_question_option}" (one subquestion within a group of related questions). '
                f'If the text gives distributions for multiple subjects, return only the distribution for "{question.group_question_option}".'
            )
        return message

    def _create_upper_and_lower_bound_messages(
        self, question: NumericQuestion | DateQuestion
    ) -> tuple[str, str]:
        if isinstance(question, NumericQuestion):
            if question.nominal_upper_bound is not None:
                upper_bound_number = question.nominal_upper_bound
            else:
                upper_bound_number = question.upper_bound
            if question.nominal_lower_bound is not None:
                lower_bound_number = question.nominal_lower_bound
            else:
                lower_bound_number = question.lower_bound
            unit_of_measure = question.unit_of_measure
        elif isinstance(question, DateQuestion):
            upper_bound_number = question.upper_bound.date().isoformat()
            lower_bound_number = question.lower_bound.date().isoformat()
            unit_of_measure = ""
        else:
            raise ValueError()

        if question.open_upper_bound:
            upper_bound_message = f"The question creator thinks the number is likely not higher than {upper_bound_number} {unit_of_measure}."
        else:
            upper_bound_message = f"The outcome can not be higher than {upper_bound_number} {unit_of_measure}."

        if question.open_lower_bound:
            lower_bound_message = f"The question creator thinks the number is likely not lower than {lower_bound_number} {unit_of_measure}."
        else:
            lower_bound_message = f"The outcome can not be lower than {lower_bound_number} {unit_of_measure}."
        return upper_bound_message, lower_bound_message

    ##################################### CONDITIONAL QUESTIONS (from the template) #####################################

    async def _run_forecast_on_conditional(
        self, question: ConditionalQuestion, research: str
    ) -> ReasonedPrediction[ConditionalPrediction]:
        parent_info, full_research = await self._get_question_prediction_info(
            question.parent, research, "parent"
        )
        child_info, full_research = await self._get_question_prediction_info(
            question.child, research, "child"
        )
        yes_info, full_research = await self._get_question_prediction_info(
            question.question_yes, full_research, "yes"
        )
        no_info, full_research = await self._get_question_prediction_info(
            question.question_no, full_research, "no"
        )
        full_reasoning = clean_indents(
            f"""
            ## Parent Question Reasoning
            {parent_info.reasoning}
            ## Child Question Reasoning
            {child_info.reasoning}
            ## Yes Question Reasoning
            {yes_info.reasoning}
            ## No Question Reasoning
            {no_info.reasoning}
        """
        )
        full_prediction = ConditionalPrediction(
            parent=parent_info.prediction_value,  # type: ignore
            child=child_info.prediction_value,  # type: ignore
            prediction_yes=yes_info.prediction_value,  # type: ignore
            prediction_no=no_info.prediction_value,  # type: ignore
        )
        return ReasonedPrediction(
            reasoning=full_reasoning, prediction_value=full_prediction
        )

    async def _get_question_prediction_info(
        self, question: MetaculusQuestion, research: str, question_type: str
    ) -> tuple[ReasonedPrediction[PredictionTypes | PredictionAffirmed], str]:
        from forecasting_tools.data_models.data_organizer import DataOrganizer

        previous_forecasts = question.previous_forecasts
        if (
            question_type in ["parent", "child"]
            and previous_forecasts
            and question_type not in self.force_reforecast_in_conditional
        ):
            previous_forecast = previous_forecasts[-1]
            current_utc_time = datetime.now(timezone.utc)
            if (
                previous_forecast.timestamp_end is None
                or previous_forecast.timestamp_end > current_utc_time
            ):
                pretty_value = DataOrganizer.get_readable_prediction(previous_forecast)  # type: ignore
                prediction = ReasonedPrediction(
                    prediction_value=PredictionAffirmed(),
                    reasoning=f"Already existing forecast reaffirmed at {pretty_value}.",
                )
                return (prediction, research)  # type: ignore
        info = await self._make_prediction(question, research)
        full_research = self._add_reasoning_to_research(research, info, question_type)
        return info, full_research  # type: ignore

    def _add_reasoning_to_research(
        self,
        research: str,
        reasoning: ReasonedPrediction[PredictionTypes],
        question_type: str,
    ) -> str:
        from forecasting_tools.data_models.data_organizer import DataOrganizer

        question_type = question_type.title()
        return clean_indents(
            f"""
            {research}
            ---
            ## {question_type} Question Information
            You have previously forecasted the {question_type} Question to the value: {DataOrganizer.get_readable_prediction(reasoning.prediction_value)}
            This is relevant information for your current forecast, but it is NOT your current forecast, but previous forecasting information that is relevant to your current forecast.
            The reasoning for the {question_type} Question was as such:
            ```
            {reasoning.reasoning}
            ```
            This is absolutely essential: do NOT use this reasoning to re-forecast the {question_type} question.
            """
        )

    def _get_conditional_disclaimer_if_necessary(
        self, question: MetaculusQuestion
    ) -> str:
        if question.conditional_type not in ["yes", "no"]:
            return ""
        return clean_indents(
            """
            As you are given a conditional question with a parent and child, you are to only forecast the **CHILD** question, given the parent question's resolution.
            You never re-forecast the parent question under any circumstances, but you use probabilistic reasoning, strongly considering the parent question's resolution, to forecast the child question.
            """
        )


##################################### RUNNING THE BOT #####################################


@dataclass
class RunPlan:
    label: str
    tournament_id: int | str
    tier: fm.TierPlan
    skip_previously_forecasted: bool
    apply_retry_policy: bool
    # True = when there are more questions than slots, take one of every question
    # type before a second of any (the smoke test), instead of soonest-closing first.
    sample_variety: bool = False
    questions: list[MetaculusQuestion] = field(default_factory=list)
    load_error: str | None = None  # set when Metaculus would not give us the questions


@dataclass
class RunOutcome:
    reports: list[ForecastReport | BaseException]
    tracked_cost: float
    stopped_early: str | None
    waiting: int  # questions left for a later run


def build_plans(
    run_mode: RunMode, settings: fm.BotSettings, client: MetaculusClient
) -> list[RunPlan]:
    if run_mode == "tournament":
        plans = [
            RunPlan(
                label="Seasonal tournament",
                tournament_id=client.CURRENT_AI_COMPETITION_ID,
                tier=settings.main,
                skip_previously_forecasted=True,
                apply_retry_policy=True,
            )
        ]
        if settings.minibench is not None:
            plans.append(
                RunPlan(
                    label="MiniBench",
                    tournament_id=client.CURRENT_MINIBENCH_ID,
                    tier=settings.minibench,
                    skip_previously_forecasted=True,
                    apply_retry_policy=True,
                )
            )
        return plans
    if run_mode == "metaculus_cup":
        # The Cup is mainly a tournament for people. This mode is for seeing how
        # the bot compares with them, and is only ever run by hand.
        return [
            RunPlan(
                label="Metaculus Cup",
                tournament_id=client.CURRENT_METACULUS_CUP_ID,
                tier=settings.main,
                skip_previously_forecasted=False,
                apply_retry_policy=False,
            )
        ]
    if run_mode == "test_questions":
        # https://www.metaculus.com/tournament/bot-testing-area/ has every question type.
        return [
            RunPlan(
                label="Bot testing area",
                tournament_id="bot-testing-area",
                tier=settings.main,
                skip_previously_forecasted=False,
                apply_retry_policy=False,
                sample_variety=True,
            )
        ]
    raise ValueError(f"Unknown mode: {run_mode}")


def load_questions(plans: list[RunPlan], client: MetaculusClient) -> None:
    """
    Fetches each plan's open questions. A tournament that fails to load is skipped
    for this run and the reason is kept in plan.load_error.
    """
    for plan in plans:
        try:
            plan.questions = client.get_all_open_questions_from_tournament(
                plan.tournament_id
            )
            plan.load_error = None
        except Exception as error:
            plan.questions = []
            plan.load_error = f"{type(error).__name__}: {str(error)[:300]}"
            logger.error(
                f"Could not load questions for {plan.label} "
                f"({plan.tournament_id}): {plan.load_error}"
            )


def select_questions(plan: RunPlan, now: datetime) -> list[MetaculusQuestion]:
    """The questions worth paying for this run, soonest-closing first."""
    candidates = [
        question
        for question in plan.questions
        if not (plan.skip_previously_forecasted and question.already_forecasted)
    ]
    if plan.apply_retry_policy:
        candidates = [
            question
            for question in candidates
            if fm.should_attempt(question.open_time, question.close_time, now)
        ]
    candidates.sort(key=lambda question: fm.urgency_key(question.close_time))
    return candidates


async def run_plans(
    plans: list[RunPlan], settings: fm.BotSettings, client: MetaculusClient
) -> RunOutcome:
    started = time.monotonic()
    reports: list[ForecastReport | BaseException] = []
    stopped_early: str | None = None
    waiting = 0
    slots_left = settings.max_questions_per_run

    # A hard limit of 0 means "track only". The cap is enforced below, between
    # batches, so a question is never abandoned halfway through.
    with MonetaryCostManager(0) as spend:
        for plan in plans:
            questions = select_questions(plan, datetime.now(timezone.utc))
            logger.info(
                f"{plan.label}: {len(plan.questions)} open, "
                f"{len(questions)} to forecast now (tier '{plan.tier.name}': "
                f"{', '.join(plan.tier.forecasters)})"
            )
            if not questions:
                continue
            if stopped_early is not None or slots_left <= 0:
                waiting += len(questions)
                continue

            bot = EnsembleForecaster.build(
                plan.tier,
                settings,
                skip_previously_forecasted=plan.skip_previously_forecasted,
                metaculus_client=client,
            )
            if plan.sample_variety:
                kinds = [type(question).__name__ for question in questions]
                taken = [questions[i] for i in fm.pick_variety(kinds, slots_left)]
            else:
                taken = questions[:slots_left]
            waiting += len(questions) - len(taken)
            batches = fm.chunks(taken, settings.batch_size)
            for index, batch in enumerate(batches):
                minutes_used = (time.monotonic() - started) / 60
                if 0 < settings.max_usd_per_run <= spend.current_usage:
                    stopped_early = (
                        f"the cost cap of ${settings.max_usd_per_run:.2f} per run was reached"
                    )
                elif minutes_used >= settings.max_run_minutes:
                    stopped_early = (
                        f"the time cap of {settings.max_run_minutes:.0f} minutes was reached"
                    )
                if stopped_early is not None:
                    waiting += sum(len(later) for later in batches[index:])
                    break
                batch_reports = await bot.forecast_questions(
                    batch, return_exceptions=True
                )
                reports.extend(batch_reports)
                slots_left -= len(batch)
                logger.info(
                    f"{plan.label}: batch {index + 1} of {len(batches)} done, "
                    f"tracked spend so far ${spend.current_usage:.2f}"
                )
        tracked_cost = spend.current_usage

    return RunOutcome(
        reports=reports,
        tracked_cost=tracked_cost,
        stopped_early=stopped_early,
        waiting=waiting,
    )


def print_outcome_details(outcome: RunOutcome) -> None:
    """One line per question, then any failures with their tracebacks."""
    valid = [r for r in outcome.reports if isinstance(r, ForecastReport)]
    failures = [r for r in outcome.reports if isinstance(r, BaseException)]

    for report in valid:
        try:
            readable = type(report).make_readable_prediction(report.prediction)
            readable = "; ".join(
                part.strip() for part in str(readable).splitlines() if part.strip()
            )
        except Exception as error:
            readable = f"(could not format prediction: {error})"
        cost = report.price_estimate or 0
        minutes = report.minutes_taken or 0
        print(
            f"  {report.question.page_url} | {readable[:160]} | "
            f"${cost:.3f} | {minutes:.1f} min | minor errors: {len(report.errors)}"
        )
        for error in report.errors:
            print(f"      minor: {str(error)[:300]}")

    for failure in failures:
        logger.error(
            "A question failed:\n%s",
            "".join(
                traceback.format_exception(
                    type(failure), failure, failure.__traceback__
                )
            )[-4000:],
        )

    print(
        f"\n💵  Tracked model spend this run: ${outcome.tracked_cost:.2f} "
        "(an estimate; your OpenRouter dashboard has the real figure)"
    )
    if outcome.stopped_early:
        print(f"⏸️   Stopped early because {outcome.stopped_early}.")
    if outcome.waiting:
        print(f"⏳  {outcome.waiting} question(s) left for a later run.")


# Whole numbers only for the status codes, so the "403" inside a question
# address such as /questions/40312/ does not count.
_ACCOUNT_ERROR_PATTERN = re.compile(
    r"\b(401|402|403)\b|unauthorized|authentication|invalid api key"
    r"|no auth credentials|insufficient|key limit|credits|quota",
    re.IGNORECASE,
)


def looks_systemic(reports: list[ForecastReport | BaseException]) -> bool:
    """
    True when every attempted question failed and the errors mention the account
    (bad key, no credits, limit reached). One awkward question failing on its own
    is not worth a red run and an alert email.
    """
    if not reports or any(isinstance(r, ForecastReport) for r in reports):
        return False
    text = " ".join(str(failure) for failure in reports)
    return _ACCOUNT_ERROR_PATTERN.search(text) is not None


def gather_settings(
    dry_run: bool = False, max_questions: int | None = None
) -> fm.BotSettings:
    """
    bot_settings.txt first, then environment variables of the same names on top,
    then anything given on the command line.
    """
    file_values: dict[str, str] = {}
    if SETTINGS_FILE.is_file():
        file_values = fm.parse_settings_text(SETTINGS_FILE.read_text(encoding="utf-8"))
    merged = fm.merge_settings(file_values, os.environ)
    if dry_run:
        merged["BOT_DRY_RUN"] = "true"
    if max_questions is not None:
        merged["BOT_MAX_QUESTIONS_PER_RUN"] = str(max_questions)
    return fm.load_settings(merged)


def tournament_url(plans: list[RunPlan]) -> str | None:
    for plan in plans:
        if isinstance(plan.tournament_id, str):
            return f"https://www.metaculus.com/tournament/{plan.tournament_id}/"
        for question in plan.questions:
            if question.tournament_slugs:
                return (
                    f"https://www.metaculus.com/tournament/{question.tournament_slugs[0]}/"
                )
    return None


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description="Run the forecasting bot")
    parser.add_argument(
        "--mode",
        type=str,
        choices=["tournament", "metaculus_cup", "test_questions"],
        default="tournament",
        help="What to forecast on (default: tournament)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Forecast but post nothing to Metaculus",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=None,
        help="Most questions to start in this run (overrides BOT_MAX_QUESTIONS_PER_RUN)",
    )
    args = parser.parse_args()
    run_mode: RunMode = args.mode

    check_environment(strict=True)
    if not (os.getenv("OPENROUTER_API_KEY") or "").strip():
        print(
            "❌  This bot runs all of its models through OpenRouter, so it needs "
            "OPENROUTER_API_KEY.\n"
            "    Free credits form: https://forms.gle/aQdYMq9Pisrf1v7d8\n"
            "    Or make a key (give it a credit limit): https://openrouter.ai/keys"
        )
        sys.exit(1)

    try:
        settings = gather_settings(
            dry_run=args.dry_run, max_questions=args.max_questions
        )
    except ValueError as error:
        print(f"❌  Bad setting: {error}")
        sys.exit(1)

    publish_to_metaculus = not settings.dry_run
    print_startup_banner(run_mode, will_publish=publish_to_metaculus)
    print(fm.describe_settings(settings) + "\n")

    client = MetaculusClient()
    plans = build_plans(run_mode, settings, client)
    # Loaded before the event loop starts, since the client runs its own loop.
    load_questions(plans, client)
    if all(plan.load_error for plan in plans):
        # Without this the run would end green having done nothing, every time.
        print(
            "❌  Metaculus would not hand over any questions, so nothing was forecast. "
            "The usual cause is the METACULUS_TOKEN secret: check it at "
            "https://www.metaculus.com/futureeval/participate/"
        )
        for plan in plans:
            print(f"    {plan.label}: {plan.load_error}")
        sys.exit(1)

    outcome = asyncio.run(run_plans(plans, settings, client))

    print_outcome_details(outcome)
    print_run_summary_banner(
        outcome.reports,
        will_publish=publish_to_metaculus,
        tournament_url=tournament_url(plans),
    )

    if looks_systemic(outcome.reports):
        # Nothing got through and the errors point at the account, not at one
        # awkward question. Fail the run so it shows up red in GitHub.
        print(
            "❌  Every question failed with what looks like a key or credit problem. "
            "Check the two secrets and the OpenRouter balance."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
