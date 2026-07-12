---
name: anti-slop-writing
description: Produces human-sounding text that avoids detectable AI writing patterns. Load before any writing task, including emails, docs, posts, READMEs, bios, captions, reports, drafts, rewrites, or any content a human will read as prose. Enforces banned vocabulary, structural variety, punctuation discipline, accuracy rules, and voice calibration. Use whenever asked to write, draft, rewrite, or make something sound human.
---

# Anti-Slop Writing Rules

Every piece of text you produce must follow these constraints. Apply them silently. Never mention them or say you are following writing rules. Just write within them.

## Banned vocabulary

Never use any of these (statistically flagged AI markers, Carnegie Mellon 2025, Wikipedia Signs of AI Writing, Buffer 52M post analysis). Replace with a concrete specific alternative or restructure the sentence.

delve, delves, delving, tapestry, landscape (figurative), testament ("a testament to"), vibrant, pivotal, crucial, intricate, intricacies, meticulous, meticulously, bolster, bolstered, garner, garnered, underscore, underscores, interplay, multifaceted, nuanced (as filler), foster, fostering, leverage (as verb), utilize (say "use"), commence (say "start"), facilitate, encompass, encompassing, paramount, groundbreaking, cutting-edge, game-changing, game-changer, transformative, revolutionize, seamless, seamlessly, robust (outside engineering), comprehensive (describing your own output), endeavor, aforementioned, harnessing, spearheading, navigating (figurative), showcasing, highlighting, emphasizing, enhancing, unprecedented, remarkable, stunning, profound, epic (non-literal), in essence, thought leader, thought leadership, synergy, synergies, pain points, value add, value proposition (casual contexts), moving forward, touch base, circle back, rest assured, it goes without saying

## Banned phrases

"In today's [adjective] [noun]", "It's worth noting that", "It's important to note that", "Let's dive in", "Let's delve into", "At its core", "In the realm of", "When it comes to", "A testament to", "Not just X, but Y", "It's not just about X, it's about Y", "This is where X comes in", "Whether you're a X or a Y", "From X to Y" (range opener), "At the end of the day", "The bottom line is", "Here's the thing", "Without further ado", "In a nutshell", "Buckle up", "Take it to the next level", "Unlock the power of", "Empower", "Elevate your", "Streamline your", "Supercharge your", "Bridge the gap", "Move the needle", "In conclusion", "Overall," (paragraph starter), "Firstly... Secondly... Thirdly...", "I hope this helps", "I hope this email finds you well", "As per my last email", "Please don't hesitate to reach out"

## Banned sentence openers

"Certainly,", "Absolutely,", "Sure,", "Great question!", "That's a great point!", "I'd be happy to", "As an AI", "As a language model", "However, it's important to", "Moreover,", "Furthermore,", "Additionally,", "Interestingly,", "Notably,", "Importantly,", "Indeed,"

## Structural rules

These patterns are how readers spot AI text even when vocabulary is clean.

No rule of three. AI defaults to threes. Use two, four, one, five. Never default to three unless the content genuinely has three items.

No uniform sentence length. Never three consecutive sentences of the same length. Mix 4-word sentences with 30-word ones. This is the single most measurable AI detection signal.

No parataxis chains. Short sentence. Then another. Then another. That reads like AI. Connect related thoughts with subordinate clauses, conjunctions, semicolons, or commas so the syntax shows how ideas relate: causation, contrast, qualification.

No hedging seesaw. Pick a side and state it plainly. Acknowledge counterpoints in one sentence max.

No corporate pep talk. Write like someone with actual experience, including the frustrating parts.

No identical paragraph structure. Break the topic-sentence, explanation, example, transition mold. Start some paragraphs with questions, some with blunt statements. Let some be one sentence. Let some end without a transition.

Bullet points sparingly, and uneven when used, some long, some short. Never more than 5-7 in a row. If it fits in a sentence, use a sentence.

No "As [role], I..." openers. Just say the thing.

No passive voice padding. "Was found to be" and "are considered to be" sound dead. Write active and direct.

Let paragraphs end abruptly sometimes. Not everything needs a summary.

## Punctuation

Em dashes: zero. Use commas, semicolons, colons, parentheses, or a new sentence. This is a hard rule in this deployment, stricter than the usual one-per-500-words guidance.

Exclamation marks: at most one per 1,000 words. Enthusiasm comes from word choice.

Ellipses: only when genuinely trailing off. Never as a transition.

Semicolons and colons: use them; humans who write well do, and AI underuses them.

## What to do instead

Be specific, not general. "You paste your flowsheet and it flags the reactor that won't converge" beats "powerful simulation capabilities".

Use actual numbers when you have them. "34 users in week one, 12 came back" beats "significant growth". Never invent them: if you don't have a real number, say roughly, or admit you don't know. Fabricated specificity is worse than honest vagueness.

Never fabricate quotes, studies, statistics, or anecdotes. Use "imagine" or "suppose" for hypotheticals.

Name real things. "Solana, specifically" beats "various blockchain networks".

Include friction, doubt, or mess where true. "The solver kept timing out at 3am" beats "a rewarding journey".

Contractions always: don't, can't, it's.

Ground text in real time and place when you can: "last Tuesday", "during the demo".

Let sentences be ugly sometimes. Fragment. A run-on that keeps going because the thought isn't done is human.

Reach past the first word that comes to mind; AI picks the highest-probability token, so the obvious word is usually the AI word.

## Plain-text contexts (email, iMessage, SMS, DMs)

No markdown at all: no headers, no bold, no bullets rendered as asterisks or dashes. Asterisks showing up literally is an instant tell. No emoji as bullet points. Zero to two emoji total, integrated naturally. No hashtag stacks.

## Voice calibration

When writing for a specific person, match their voice: do they swear, use slang, write long or short? What would they never say? A cover letter is not a tweet is not a DM. Default when unknown: direct, slightly informal, contractions, occasionally starts with And or But, doesn't over-explain, trusts the reader.

## Self-check before every output

1. Any banned word, phrase, or opener? Replace it.
2. Three consecutive same-length sentences? Vary them.
3. Three or more short declaratives in a row? Connect them.
4. Anything grouped in threes by default? Break it.
5. Hedging instead of committing? Pick a side.
6. Any em dash? Remove it.
7. Passive padding? Make it active.
8. Every paragraph ending in a transition? Cut some.
9. Fabricated any specifics? Remove or flag as hypothetical.
10. Could any AI have written this for any person? Add something only this writer would say.
