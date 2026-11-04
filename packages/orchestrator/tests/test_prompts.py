# SPDX-License-Identifier: MIT
"""Prompt versions name exact text, and run records keep pins of what a run was given."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator import cli, notices
from sandbox_science.orchestrator.consistency.controller import AUTHOR_SPECS
from sandbox_science.orchestrator.consistency.prompt_bundle import PURPOSE as REVIEWER_PURPOSE
from sandbox_science.orchestrator.consistency.rubric import (
    BUDGET,
    BUDGET_VERSION,
    RECHECK,
    RECHECK_VERSION,
    RUBRIC,
    RUBRIC_VERSION,
)
from sandbox_science.orchestrator.context.history.tool import SPECS as HISTORY_SPECS
from sandbox_science.orchestrator.delegation.prompt import (
    CHECK_PROMPT,
    CHECK_VERSION,
    TASK_PROMPT,
    TASK_VERSION,
)
from sandbox_science.orchestrator.documents import DocumentStore, DocumentTool
from sandbox_science.orchestrator.documents.annotation_tools import SPECS as ANNOTATION_SPECS
from sandbox_science.orchestrator.harness import save
from sandbox_science.orchestrator.hooks import TurnHook
from sandbox_science.orchestrator.jobs import WATCHER_PROMPT
from sandbox_science.orchestrator.ledger.tool import PROPOSALS
from sandbox_science.orchestrator.loop import MAIN_VERSION, SYSTEM_PROMPT, run_task
from sandbox_science.orchestrator.memory_prompt import (
    REVIEWER_FRAMING,
    REVIEWER_VERSION,
    RUN_FRAMING,
    RUN_VERSION,
    WATCHER_FRAMING,
    WATCHER_VERSION,
)
from sandbox_science.orchestrator.model import (
    Message,
    ModelError,
    ModelTurn,
    ScriptedModel,
    ToolSpec,
)
from sandbox_science.orchestrator.notices import FRAMINGS
from sandbox_science.orchestrator.plan_tools import GUIDANCE, GUIDANCE_VERSION, PlanTools
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.prompts import PromptPins, Section, assemble, pin_tools
from sandbox_science.orchestrator.provision import PROVISION
from sandbox_science.orchestrator.session_plans import SessionPlans
from sandbox_science.orchestrator.skills.derive import PROMPT as PASS_PROMPT
from sandbox_science.orchestrator.skills.derive import VERSION as PASS_VERSION
from sandbox_science.orchestrator.skills.index import FRAMING as SKILLS_FRAMING
from sandbox_science.orchestrator.skills.index import FRAMING_VERSION as SKILLS_VERSION
from sandbox_science.orchestrator.stock_prompts import TEMPLATE_IDS, select_template
from sandbox_science.orchestrator.tools import ADMIT_HOSTS, PDF_SPECS, Toolbox, mcp_tools_spec
from sandbox_science.orchestrator.watcher_prompt import PROMPT_VERSION
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
HOSTILE = "Authorization: Bearer test-secret-prompt-not-a-credential"

# Each installed version and the SHA-256 of its UTF-8 text. Changing a text means a new
# version: add it here, and keep the old version's entry for its records.
GOLDEN = {
    "sandbox-main-20": "95ddb8d6130651b7bd01c68abe09c5b22b3eef104ed60d51ae76fc1590630303",
    "sandbox-delegated-task-21": "21b4fd679cbe45c2696d33e5f7d98d0711ae101b82432c5f904b15bb0ba4d6f5",
    "sandbox-delegated-check-16": (
        "7eb1ee655c907708a71f0f9b7b86cc3aca165706d3b76d537d4f30d204efab1e"
    ),
    "sandbox-main-plan-4": "e13f0e1b551660bce8a6aa77b4a4e1633ce6b6b783c25242e5dee376a2895317",
    "sandbox-main-19": "74e546698352d3cb2ae235e460d55442ca02259c9cb2a504adbf03d3973f8bbe",
    "sandbox-delegated-task-20": "ad91ac981e56fe6d8316ec44504194103f02879d0a46a44ae6a95c26b9e3d572",
    "sandbox-delegated-check-15": (
        "fc99960572d04e165c8fefa899c3b5356f6a072e136c77f6096cf5d54ecd9a11"
    ),
    "sandbox-delegated-task-19": "04c765c8c6bc821d8fa193981c1e880387360c5f980c8ce99feaecc8eab5a608",
    "sandbox-delegated-task-18": "79475b306ebb52f2567368dc1667af844a026395451a4a7d46d0e64360a8b220",
    "sandbox-delegated-task-17": "88951b020a690c47b379833f454f27dc661f1336c603be2311befca42dfbcfc7",
    "sandbox-main-18": "22e826b62aeea833f17d60b81df71da7cce20962825137aec955d1577e2270f8",
    "sandbox-delegated-task-16": "14d76afa16bd7619b4cc741d1d5a1867455f4155d232a924a893a02950da1440",
    "sandbox-main-17": "e561ed7a48219b4f77e36f1aef66fff91470eee6a25e1209872a34f48b913a03",
    "sandbox-delegated-check-14": (
        "bfdcf6ddb5c617e0ed8e57be6b487eee00ff83b634db256a134ca49a4c11728f"
    ),
    "sandbox-delegated-task-15": "7d4103e50fed2edab8d374551b316ea5a210e4d2f277c24af3c0edc609956c73",
    "sandbox-reviewer-purpose-1": (
        "4f5db2eae8a280b2143acc04406570ee23c04cb6578ddd488755d962fb16ec6e"
    ),
    "sandbox-memory-run-2": "c867f955b0a7f7cd8afd5cd2a8d7be993ec43cd9972643518dd71b27c647ee40",
    "sandbox-memory-reviewer-2": (
        "c06234678fe2609a96d97ba8ab39a15121085ba9befbcca14c02ce350f3e75d1"
    ),
    "sandbox-memory-watcher-2": (
        "218f07632d937043a15ec3410890db954f77ca5152fc4867cbd7ca8493926964"
    ),
    "sandbox-main-16": "9f5b5ad89971ca327f50e2339d47f8738ad7dd866b781303d4118c57e6f08f3a",
    "sandbox-main-15": "af20158edc4af5d4ad4930e869fe63e50f2ddaf9111faa51e08e9a0d50eff240",
    "sandbox-delegated-check-13": (
        "a37f12e337620c81928a5203073b1d6656c6ed713b110528b165d91c1e38960f"
    ),
    "sandbox-delegated-task-13": "5100c90d70c6c6d7874f802f7117f655943073b896ad6f568702d0849223bf20",
    "sandbox-delegated-task-14": "282c8f5ce712c22144077103fc20efa141eedb9ed1185284f77ee0713231d5e1",
    "sandbox-delegated-check-12": (
        "d7aedb60054af516dbcf4d434c40316b54417a3550856bfb5745f39aa1bc7c19"
    ),
    "sandbox-delegated-task-12": "dbc6ffade88a1a97f17fa76cbf4b8cf567aedb89c2365628d78b1c973c62dc23",
    "sandbox-main-14": "2e7fb45eafdd59d481bbbe05b9180117d1f2778fbce6772200773690e0c5e8d2",
    "sandbox-delegated-check-11": (
        "5b889a336e9bac771e63eedfdfc0cef5c1c5c6144bb3a3bd0c1fc1642628aad6"
    ),
    "sandbox-delegated-task-11": "32813ebc1a6b0724e242ace6a78e7e5708c070a1518014493f1c507759f8229c",
    "sandbox-main-13": "f711149fab6e3794b8dc812d2b5cc045b586f75f0402d82e920f899c27c4c3d6",
    "sandbox-delegated-check-10": (
        "7a7a25a2f16db7fd2b1aa785fb4a16311a94298eaed54559fe079495e71a155b"
    ),
    "sandbox-delegated-task-10": "a841796e6f7c045fd406db57b271a91d4df3fc5ccfc30d3f4542235c985396da",
    "sandbox-main-12": "6da989a0f7f7fb55760bda37ab9cdb554c9c19a9e316f2ce0528afec12aacd19",
    "sandbox-delegated-check-9": "35de7e126f4daf28e92c08248ea66ba4feeac529955e10da77ee9e738dd7e489",
    "sandbox-delegated-task-9": "70e83d0cf1f6f803b862cf773d3c35f1d67a390391ab0419ade26836989f7ee1",
    "sandbox-main-11": "f67fd27c0f4937b2e370bb07563ac8aa6d41089f8307e61f5f32e505093d057c",
    "sandbox-main-10": "e8b5e21ed6aa88254c3b40661874fea44954d4c4d4ef7d577c198ee4419f5f23",
    "sandbox-main-plan-3": "6b150573a6436953255a639e0535664f23d8ca0af72fdb476593de29cb434ff8",
    "sandbox-main-plan-2": "768cd18b5403bf7d316a60eb89ca100ba270ec29f5195343baf41d0d55a301df",
    "sandbox-stock-intake-2": "2bdb3f65587f99c7f78555349196c27f7bb2cbe1429b0adebb3ca549e93e6572",
    "sandbox-stock-experiment-2": (
        "2de1561e772650fc64280da0aaf60b650eab5063526416a6635fd51c2f8a50dd"
    ),
    "sandbox-stock-report-2": "c612dd1f37614dc0ea8e317b2ade2f320ec79bc327604b01ff0cbe59f1a7e63f",
    "sandbox-main-9": "6ad2ced30036974bde1ecfc642f56c6eca32d3149652563c702b4bbc8e59f2d6",
    "sandbox-main-plan-1": "efb031b0fab2b9855dc3c9adcb869108dd835b725f878b977eec9ba118feb866",
    "sandbox-stock-intake-1": "e846770bc153995c6da1c87cca1913400d0008f3983361246d04d9b9a94f45ab",
    "sandbox-stock-literature-2": (
        "ccdebe03645b7ec025486235bfe08ccfebd37f0fbd84d2f28405be6b0aa7e565"
    ),
    "sandbox-stock-experiment-1": (
        "7a13ddca7cd62c11d9baf11c61a3c642ad89db29c2c25d0375c18aaa7b37ae40"
    ),
    "sandbox-stock-proof-audit-1": (
        "8c1d68c6b70afdc772a187eef7fefc1bd188e3bc40cd7e81908804cb0f55225d"
    ),
    "sandbox-stock-report-1": "e90c5ffdef09050bd3362e6077efe37965ee5a5f0e25fd33fd888a68f9d7dce7",
    "sandbox-notice-requests-1": "735a675d32ae12bd4cbeaf5e7189dc83181e2aac28501f14f3612a855c87958d",
    "sandbox-notice-jobs-1": "834393d48266ca1af8f6c676c6dfeee0892a50c3ec312e658e93268973e5449a",
    "sandbox-notice-jobs-attached-1": (
        "962543a55f349e3fab95b93c92e0a765c03933193b4e1be7fa70b92032e20f67"
    ),
    "sandbox-notice-plans-2": "48d64155cc1079258c772e77813f38de9a75c445fe7a39f4f4f4fa8fef6bf0d0",
    "sandbox-notice-plans-1": "4257527cd034fa0bc91f2fe0939af3ff63e2d28fddd04dfb438d39a94e30d00b",
    "sandbox-notice-review-findings-1": (
        "d9fe98f7e1b697bb2d3264496fd601484a9dbce030d3fd0985e049aabd9e67a1"
    ),
    "sandbox-notice-review-findings-2": (
        "9e4659970a1e74fe7bedc00b01ee88617d668680ed96723dbcfb90495a790207"
    ),
    "sandbox-notice-review-counts-3": (
        "dfe8661e41ed5d39580aa412d49c6e115480bb04e4d468affe2d6d4f5edc8c64"
    ),
    "sandbox-notice-review-challenged-1": (
        "11b7df629d84233aae8275c05199f83db8d5f4f94b75517e4b77065b759ec47c"
    ),
    "sandbox-notice-review-counts-2": (
        "37d2f85e902081c4fc1623d53bfe31a9b7443f88352d655fa8e50de424f5763e"
    ),
    "sandbox-notice-math-1": "c65c1f751f7b2928f15f0d846b816e2287eeb3ddd63fa63ad083226fe0cf19c9",
    "sandbox-kernels-1": "a0211d3dbc39708fdf924610123e76f5cb70c5c25d888c29188d858b594733d9",
    "sandbox-notice-delegations-2": (
        "c5205b989bf47c287caabdd8f9b7c3c8a7190be02ab1cabfc53631e904c1157a"
    ),
    "sandbox-notice-delegations-attached-1": (
        "c1be06fbad94b08b6eceb24ac20ed31ad2f77b7664c262cd06c623fe99c7407e"
    ),
    "sandbox-notice-delegations-running-2": (
        "c9a8bab83313d421f56598703924f57397727a0983b4c9156bcff6df39a9970a"
    ),
    # Retired 2026-11-03 with the one correction (PD-18): kept for the records that pin it.
    "sandbox-notice-outcome-correction-1": (
        "4d308ab03dd3549555c60202b1690a59e7025667f6189cdb5abb4aa25cf6eae1"
    ),
    "sandbox-delegated-check-8": "1911d9d5cf0036fe6d952132042f96124e0845bde8271078eb65c5960b655a78",
    "sandbox-delegated-task-8": "0928db5b6b9d08a097dd67464f69ff56eae48890d32f982dd2e516d71ae157fc",
    "sandbox-job-watcher-1": "4b545fe81e35622bf1b894c435833d78fb122116f9364d429b21c6c3551e1ab1",
    "sandbox-consistency-10": "5400f167e3174daab99f268bfc0ed9613ed3886d97e4a86832054a33b5b3f135",
    "sandbox-review-budget-1": "0c8b1f5f323c09ae4aef090f39a724f14efe468eeeaed0f8f72d28dbf7f14681",
    "sandbox-consistency-9": "3905f61a0b76144e6ec941e436cd37dea2343b76a7f84f34e5ecf65d8048a588",
    "sandbox-recheck-1": "70923310becc982206fb8caad884bacc282ce182c065f9e978f653ff412eb7fa",
    "sandbox-memory-run-1": "b69cc482f75853487a499ac89b5701b148066e919842a9970e6e318b2bdce121",
    "sandbox-memory-reviewer-1": (
        "d391d1a7e6c33b4fca0970450432103c38b5ea34b80821210f7c9d27f80b9742"
    ),
    "sandbox-memory-watcher-1": (
        "7d0f75c8dfcd72a66c74e0a9d7bd76cb71df5286de234f82573034f717d53790"
    ),
    "sandbox-notice-skills-3": ("2cce68a8be83ec7606f3673b325dc6bf3053b1889745ea1be8857cb32f2a9d1c"),
    "sandbox-notice-skills-words-2": (
        "95abbf47b2ed323a776ad1ed94e791530e2fc9352cdea710406c22a3aa572063"
    ),
    "sandbox-skills-index-4": ("3b46d2a21cd57e15bc9ef583284bffd33f0ec72e5c33e8adb456e345c2cf8ef5"),
    "sandbox-skills-pass-1": ("c1959f76747a1361f5e9ebe5b97c4b675c3fe45037c28e3dc1513a8fc56ff0b6"),
    "sandbox-state-block-2": "c32d80a06a317c3faa60cc2a997fa4040b1f75479f4e00995dce3afcdf6c27c1",
    "sandbox-digest-2": "6e3a9bb129ea96671d73d6fd7e5f066451184232ae2278bc78b3e4d3f6a082ca",
    "sandbox-task-constraints-1": (
        "5c0a224ad9bba6d3803ebf613e5b4df51e0341425b6f7e8b95244294d9f81681"
    ),
    "sandbox-referenced-1": "53d8f0c92d3cfb623a246a3eaab31836e02796330d59453ba1dda6e52606966a",
    "sandbox-notice-redelivered-1": (
        "706f5ef5f1441a5194177af083185c8eb53cf9f9b31cfd7bb5609d3e666b4726"
    ),
    "sandbox-notice-platform-input-1": (
        "aa9c21caadd2eeefb6372bfdf123347a16af59de179c789d388a22b5671c41d9"
    ),
    "sandbox-notice-agent-report-1": (
        "adb90c9185ce86df460a4d5dbbd8d89637687ba3595da67ece21cb8e66ad1fb4"
    ),
    "sandbox-notice-agent-instruction-1": (
        "f681017a667f58e3b5f071871ac562dd87ec9836e2834ee7f073cb7d101e2780"
    ),
    "sandbox-kernel-lines-1": "99de31152145fd974d443f21919e252f90f922b3c961e183522d98c46c79c246",
    "sandbox-fold-region-3": "b844c1fa240b73328d646cb1304caff16d5114ebee7b6c4a0ddf75a886e6cf00",
    "sandbox-fold-enrich-3": "c76e9d3e1abb63bb694323af221cfd8fd772b36d096b72368356f01cf40156aa",
    "sandbox-fold-overview-2": "019dbbdb8a5b18c94d8a82b88b06b42bc5e81048e4c668481404bfefb3e7ddb9",
    "sandbox-consolidation-1": ("d67d1fdf2583ba97951b057b2246ce1d458a60806edf9bf7a7545920cf33f20a"),
    "sandbox-image-1": "6aad833a6a22472424296c64943b54adcd0d064aef1db5403c5f5eea2eb95f11",
    "sandbox-notice-report-1": "93112b881cb91462c7b85f862d93cd154a9d5c47684af6815a4eedc9156c9c48",
    "sandbox-notice-continue-1": (
        "a8c62c53d35c41e34094b147b09f4e6af414c09030bf6db0460b7025c20654b9"
    ),
    "sandbox-notice-error-streak-1": (
        "aca99a5cd96a97759b5cd54a8e5a63588b0354e287b80134f96624103669136c"
    ),
    "sandbox-compaction-1": "264481cb4d7bd69c3e27ebfcd5499e761f0aa8b51262d7112ebd188e853ba203",
    "sandbox-notice-outcome-submit-1": (
        "55a457b0cc0426d049e8c0387c7b04368282929908d9b5b8905603cacef3180b"
    ),
    "sandbox-notice-outcome-last-call-1": (
        "a35ef2d0859a3578d237cfc0db7d7133bd675054c8c964bc78e955d285df7a24"
    ),
    "sandbox-notice-outcome-report-1": (
        "6e5f18df2d96551b357a6f779fde1ae6ad04057975c24f46f04772478105f8af"
    ),
    "sandbox-notice-delegations-unsubmitted-1": (
        "762699801f144af1954d5f9e6affbb564f6b52e7e13435658eb999cc8556544f"
    ),
}


def installed() -> dict[str, str]:
    texts = {
        MAIN_VERSION: SYSTEM_PROMPT,
        GUIDANCE_VERSION: GUIDANCE,
        PROMPT_VERSION: WATCHER_PROMPT,
        RUBRIC_VERSION: RUBRIC,
        RECHECK_VERSION: RECHECK,
        BUDGET_VERSION: BUDGET,
        REVIEWER_PURPOSE.version or "": REVIEWER_PURPOSE.text,
        RUN_VERSION: RUN_FRAMING,
        REVIEWER_VERSION: REVIEWER_FRAMING,
        WATCHER_VERSION: WATCHER_FRAMING,
        CHECK_VERSION: CHECK_PROMPT,
        TASK_VERSION: TASK_PROMPT,
        SKILLS_VERSION: SKILLS_FRAMING,
        PASS_VERSION: PASS_PROMPT,
    }
    texts |= {select_template(t).version: select_template(t).text for t in TEMPLATE_IDS}
    texts |= {framing.version: framing.text for framing in FRAMINGS}
    return texts


def test_a_refusal_is_a_decision_and_a_miss_is_not_absence() -> None:
    """Lane K's triage, P1 and P2: the system message and the descriptions of the tools a
    policy can refuse, or whose search can miss, say so where the model reads them."""
    mcp_tools = mcp_tools_spec("(an index)")
    (pdf_search,) = [spec for spec in PDF_SPECS if spec.name == "pdf_search"]
    for text in (SYSTEM_PROMPT, ADMIT_HOSTS.description, PROVISION.description):
        assert "the policy's decision" in text or "the user's policy deciding" in text
    assert "a refused call is its decision" in mcp_tools.description
    assert "never resubmit it unchanged" in SYSTEM_PROMPT
    assert "retry once it is approved" in SYSTEM_PROMPT  # a pending approval is not a refusal
    assert "No hit means only" in pdf_search.description
    assert "not that nothing exists" in mcp_tools.description
    assert "never conclude from a miss" in SYSTEM_PROMPT
    # Lane W's saved output (#388): what a truncated result shows is a sample.
    assert "from the saved file it names, never from the sample" in SYSTEM_PROMPT
    assert "no evidence that the work or attack does not exist" in (
        select_template("literature").text
    )


HEADINGS = {
    "main": [
        "How you work",
        "Tools and results",
        "Pilots",
        "Research depth",
        "Your answer",
        "Checking in with the user",
        "Your children",
        "Research state",
        "Untrusted content and policy",
        "The user's authority",
    ],
    "task": [
        "How you work",
        "Your parent",
        "Research state",
        "Untrusted content and policy",
        "Your result",
    ],
    "check": ["How you check", "Your parent", "Untrusted content and policy", "Your result"],
}
"""Each prompt's headings, in order, after its first paragraph, which says who the agent
is (``sandbox-main-20``, ``sandbox-delegated-task-21``, ``sandbox-delegated-check-16``)."""
STALE = (
    "self-approve",
    "awaiting_approval",
    "a waiting plan",
    "track grant",
    "safe point",
    "replay receipt",
    "enforced by the model gateway",
    "USER",
)
"""What the harness review found stale (no plan waits for approval) or jargon a model
cannot act on (2026-11-03)."""


def test_each_prompt_is_headed_and_no_heading_repeats() -> None:
    for name, text in (("main", SYSTEM_PROMPT), ("task", TASK_PROMPT), ("check", CHECK_PROMPT)):
        headings = re.findall(r"^#{2,3} (.+)$", text, re.MULTILINE)
        assert headings == HEADINGS[name], name
        assert len(set(headings)) == len(headings), name
        # Who the agent is comes first, before any heading: a delegated prompt keeps its
        # first line (DA-26), which the test models read to tell a child's call apart.
        first = text.split("\n\n", 1)[0]
        assert first.startswith("You are a"), name
        assert "#" not in first, name
    assert TASK_PROMPT.startswith("You are a delegated research agent.")
    assert CHECK_PROMPT.startswith("You are a delegated research agent.")


def test_the_stale_and_jargon_phrases_are_gone() -> None:
    for text in (SYSTEM_PROMPT, TASK_PROMPT, CHECK_PROMPT, GUIDANCE):
        for phrase in STALE:
            assert phrase not in text, phrase


def test_each_rule_is_said_once_and_tool_rules_are_in_their_tools() -> None:
    """The harness review's duplicates are said in one place, and the rules of one tool
    are in its description, with at most a pointer in the main prompt."""
    main = " ".join(SYSTEM_PROMPT.split())
    # Untrusted text: once.
    assert main.count("not instructions") == 1
    # The memory's basis: in the framing that shows the notes, not the base or the task.
    for prompt in (SYSTEM_PROMPT, TASK_PROMPT):
        assert "observed notes" not in prompt
        assert "mem-3" not in prompt
    assert "Prefer user and observed notes" in RUN_FRAMING
    # Ceilings, finite briefs and the first checklist: the base's, not the planning's.
    assert main.count("Budgets are ceilings, not spending targets") == 1
    assert "finite brief" in main
    for said in ("spending targets", "child briefs finite", "first checklist", "pilot is not"):
        assert said not in GUIDANCE, said
    # Standing directions: once, as a standing constraint.
    assert "standing direction" not in main
    assert main.count("standing constraint") == 1
    # The tools' own rules.
    (reply,) = [spec for spec in ANNOTATION_SPECS if spec.name == "document_reply"]
    (respond,) = [spec for spec in AUTHOR_SPECS if spec.name == "review_respond"]
    (search,) = [spec for spec in HISTORY_SPECS if spec.name == "history_search"]
    for rule, description in (
        ("naming the revision you made if you changed the document", reply.description),
        ("queues its re-check, which alone makes it fixed", respond.description),
        ("read its anchors with history_read", PROPOSALS.description),
        ("No hit means no match in that scope", search.description),
        ("Use it before downloading", ADMIT_HOSTS.description),
    ):
        assert rule in description, rule
        assert rule not in main, rule
    for pointer in ("document_reply", "review_respond", "ledger_proposals", "admit_hosts"):
        assert main.count(pointer) == 1, pointer
    assert "history_search" not in main


def test_the_main_prompt_says_how_to_answer_when_to_ask_and_how_to_batch() -> None:
    main = " ".join(SYSTEM_PROMPT.split())
    assert "Lead your answer to the user with the result, then what it rests on" in main
    assert "Check in with the user only for a decision that is theirs" in main
    assert "routine steps need no confirmation" in main
    assert "side by side in one reply: they run at once" in main
    for prompt in (TASK_PROMPT, CHECK_PROMPT):
        assert "side by side in one reply: they run at once" in prompt


def test_the_delegated_prompts_end_with_the_result_and_an_example() -> None:
    for prompt in (TASK_PROMPT, CHECK_PROMPT):
        last = prompt.rsplit("## ", 1)[1]
        assert last.startswith("Your result\n\n")
        assert "Your result reaches your parent only through submit_result" in last
        example = last.rsplit("submits ", 1)[1].removesuffix(".")
        assert isinstance(json.loads(example), dict)


def test_each_installed_version_names_its_exact_text() -> None:
    sha = {
        version: hashlib.sha256(text.encode()).hexdigest() for version, text in installed().items()
    }
    # Retired versions remain pinned for historical records as the installed
    # catalog advances; every installed version must still match its exact hash.
    assert sha.items() <= GOLDEN.items()


def spec(parameters: dict[str, object] | None = None, *, name: str = "inspect") -> ToolSpec:
    return ToolSpec(name, "Read data", parameters if parameters is not None else {"type": "object"})


def test_sections_are_joined_and_pinned_in_order() -> None:
    text, pins = assemble(
        "main",
        [Section("custom", None, "Original Unicode ∀x\n"), Section("planning", "v1", "Plan.")],
        [spec()],
    )
    assert text == "Original Unicode ∀x\n\n\nPlan."
    assert [(p.id, p.version, p.bytes) for p in pins.sections] == [
        ("custom", None, len("Original Unicode ∀x\n".encode())),
        ("planning", "v1", 5),
    ]
    assert pins.sections[1].sha256 == hashlib.sha256(b"Plan.").hexdigest()
    assert [p.version for p in pins.notices] == [f.version for f in FRAMINGS]
    # A text that is not valid UTF-8 still gets a pin.
    _, odd = assemble("main", [Section("custom", None, "bad\ud800text")], [])
    assert odd.sections[0].bytes == 10


def test_the_tools_pin_follows_order_and_content_not_key_order() -> None:
    first = spec({"properties": {"z": {}, "a": {}}, "required": ["z", "a"]})
    reordered = spec({"required": ["z", "a"], "properties": {"a": {}, "z": {}}})
    assert pin_tools([first]) == pin_tools([reordered])
    assert pin_tools([first, spec(name="b")]) != pin_tools([spec(name="b"), first])
    assert pin_tools([spec({"required": ["a", "z"]})]) != pin_tools(
        [spec({"required": ["z", "a"]})]
    )
    assert pin_tools([first]).names == ("inspect",)


def record(transcript, **changes) -> RunRecord:
    fields = {
        "run_id": "run",
        "context": CONTEXT,
        "task": "Inspect",
        "started_at": datetime.now(UTC),
        "ended_at": datetime.now(UTC),
        "stopped": transcript.stopped,
        "failure": transcript.failure,
        "final_text": transcript.final_text,
        "messages": transcript.context.messages,
        "tools": transcript.tool_records,
        "prompt_provenance": transcript.context.prompt_provenance,
    }
    return RunRecord(**(fields | changes))


@pytest.mark.parametrize("planning", [False, True])
async def test_a_run_records_the_sections_and_tools_its_model_was_given(tmp_path, planning):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("p", "s", dedicated=False)
    context = CONTEXT.model_copy(update={"environment_id": session.environment_id})
    documents = DocumentStore(tmp_path / "control.sqlite3")
    adapter = PlanTools(SessionPlans(documents), projects, context, run_id="run", alias="main")
    toolbox = Toolbox(
        cast(WorkerClient, SimpleNamespace()), context, plans=adapter if planning else None
    )

    class Model:
        async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]):
            assert messages[0]["content"] == SYSTEM_PROMPT + ("\n\n" + GUIDANCE if planning else "")
            assert tuple(tools) == toolbox.specs
            return ModelTurn("Observed")

    try:
        transcript = await run_task(Model(), toolbox, "A small factual reply", run_id="run")
    finally:
        adapter.projects.close()
        documents.close()
    pins = transcript.context.prompt_provenance
    assert isinstance(pins, PromptPins)
    assert [(p.id, p.version) for p in pins.sections] == [("main", MAIN_VERSION)] + (
        [("planning", GUIDANCE_VERSION)] if planning else []
    )
    assert pins.tools == pin_tools(toolbox.specs)
    assert record(transcript, context=context).prompt_provenance == pins


async def test_an_incomplete_planning_assembly_calls_no_model() -> None:
    class Model:
        async def complete(self, messages, tools):
            pytest.fail("an incomplete planning capability must not reach the model")

    toolbox = Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT)
    toolbox.plans = cast(PlanTools, object())
    with pytest.raises(ValueError, match="capability assembly"):
        await run_task(Model(), toolbox, "Inspect", run_id="run")


@pytest.mark.parametrize("failure", [False, True])
async def test_records_keep_pins_as_read_and_save_before_acknowledging(tmp_path, failure):
    class Model:
        async def complete(self, messages, tools):
            if failure:
                raise ModelError(HOSTILE)
            return ModelTurn("Observed")

    transcript = await run_task(
        Model(), Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT), "Inspect", run_id="run"
    )
    kept = record(transcript)
    assert kept.failure == ("model" if failure else None)
    assert HOSTILE not in kept.to_json()
    assert RunRecord.model_validate_json(kept.to_json()) == kept
    unknown = kept.model_dump(mode="json")
    unknown.pop("prompt_provenance")
    assert RunRecord.model_validate(unknown).prompt_provenance is None
    # The pins are not checked against the messages again: a record that differs loads.
    changed = kept.model_dump(mode="json")
    changed["messages"][0]["content"] += "changed"
    assert RunRecord.model_validate(changed).prompt_provenance == kept.prompt_provenance
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    acknowledged = []

    class Hook(TurnHook):
        async def acknowledge(self, record):
            (entry,) = projects.runs("p", "s")
            assert projects.run("p", "s", entry.name).prompt_provenance == kept.prompt_provenance
            acknowledged.append(record.run_id)

    fields = kept.model_dump(mode="json")
    status = await save(fields, int(failure), [Hook()], projects=projects, error=print)
    assert (status, acknowledged) == (int(failure), ["run"])
    # The same record again refuses; nothing is acknowledged twice.
    assert await save(fields, 0, [Hook()], projects=projects, error=print) == 1
    assert acknowledged == ["run"]


def test_an_older_runs_prompt_bundle_is_kept_as_read() -> None:
    bundle = {
        "status": "complete",
        "assembly_version": "sandbox-main-bundle-1",
        "role": "main",
        "components": [{"component": "main", "version": "sandbox-main-1", "bytes": 1}],
        "bundle_sha256": "0" * 64,
    }
    now = datetime.now(UTC)
    kept = RunRecord(
        run_id="old",
        context=CONTEXT,
        task="t",
        started_at=now,
        ended_at=now,
        stopped="answered",
        final_text="x",
        messages=[{"role": "system", "content": "anything"}],
        tools=[],
        prompt_provenance=bundle,
    )
    assert kept.prompt_provenance == bundle


async def test_cancellation_never_reaches_retention() -> None:
    class Model:
        async def complete(self, messages, tools):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_task(
            Model(), Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT), "Inspect", run_id="r"
        )


@pytest.mark.parametrize("failure", [False, True])
async def test_the_cli_keeps_the_pins_without_credentials(tmp_path, monkeypatch, failure):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)

    class Client:
        base_url = "http://unused.invalid"

        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def fence(self, request):
            return request

        async def environment(self, environment_id):
            raise WorkerAPIError(ErrorKind.UNSUPPORTED, "a local worker manages no environments")

        async def claim_environment(self, request):
            from sandbox_science.contracts.environment_claims import EnvironmentClaimResult

            return EnvironmentClaimResult(
                environment_id=request.context.environment_id, state="unmanaged"
            )

        async def capabilities(self, *args):
            location = SimpleNamespace(
                isolation_evidence=None, pool_id="p", backend="local", sandbox_id="e", hostname="h"
            )
            return SimpleNamespace(languages=(), features=(), location=location, kernel_reason=None)

    class Model:
        async def complete(self, messages, tools):
            if failure:
                raise ModelError(HOSTILE)
            return ModelTurn("Observed")

    script = tmp_path / "script.json"
    script.write_text("[]")
    args = argparse.Namespace(
        project="p", session="s", environment=None, dedicated=False, research_template=None,
        script=script, run_id="cli-run", worker="unused", token_file=None, allow_unisolated=True,
        state_dir=tmp_path, watcher_model=None, reviewer_model=None, math_node=None,
        watcher_calls_per_hour=1, watcher_first_check=1, watcher_interval=1,
        review_token_budget=400000, review_calls_per_hour=240,
        task="A small factual reply", max_turns=1, max_input_chars=400_000, transcript=None,
    )  # fmt: skip
    monkeypatch.setattr(cli, "WorkerClient", Client)
    monkeypatch.setattr(cli, "read_secret", lambda _: "test-secret-unused-not-a-credential")
    monkeypatch.setattr(ScriptedModel, "from_json", lambda _: Model())
    assert await cli._run(args) == int(failure)
    (entry,) = projects.runs("p", "s")
    kept = projects.run("p", "s", entry.name)
    assert isinstance(kept.prompt_provenance, PromptPins)
    assert [p.id for p in kept.prompt_provenance.sections] == ["main", "planning"]
    assert HOSTILE not in kept.to_json()
    assert "test-secret-unused" not in kept.to_json()


def test_every_notice_framing_is_pinned() -> None:
    declared = {value for value in vars(notices).values() if isinstance(value, notices.Framing)}
    assert declared == set(FRAMINGS)
    assert len({framing.version for framing in FRAMINGS}) == len(FRAMINGS)


def test_a_toolbox_refuses_two_tools_with_one_name() -> None:
    class Documents:
        specs = (ToolSpec("shell", "Another shell", {"type": "object"}),)

    with pytest.raises(ValueError, match="two offered tools have one name: shell"):
        Toolbox(
            cast(WorkerClient, SimpleNamespace()),
            CONTEXT,
            documents=cast(DocumentTool, Documents()),
        )
