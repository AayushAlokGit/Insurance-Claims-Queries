# Adaptional Claim Analysis Exercise

At Adaptional we build AI agents for insurance. One of the biggest challenges in our world is that insurance claim data is unstructured, technical, and messy. You will build a mini-version of our claims parsing feature that reads unstructured claim notes and turns them into a structured, queryable dataset.

## Goal

Given a representative workers comp claim, please create a system to ingest the claim and parse it into a queryable data structure. You will have 2 sample claims to test on, but we want to build a system that could parse a large body of similar claims and support queries over the entire corpus to discover insights and trends. AI can help you understand what a workers comp claim is and what data points you may want to extract.

## Functional Requirements

You should build a service that can:

- Ingest a claim file and parse the data inside it
- Store the data
- Query the data (humans or AI-written queries)

Think about building a well-organized codebase that has room to grow in the future as we expand support for more claims and query patterns. This is an open-ended exercise with different valid approaches. You can use as much AI or web research as you’d like. As a starting point, focus on supporting the sample queries below. A good strategy to start is working backwards:

- What queries do you want to support?
- What facts or data points are required to answer those queries?
- How can you extract those data points from unstructured notes as reliably as possible?

### Scope

You are not expected to parse every possible detail from a claim or support every possible query. Just as-if you were building a new feature in production, your goal should be to prove value by supporting a starting set of useful queries and build a stable foundation to expand on. You will scope the project to get to a working deliverable within the time that you can allocate.

### Sample Queries (Start Here)

1. How long did it take for the employee to return to work?
2. How many appointments were attended?
3. How many times did our reserve change? By how much?
4. How long does it take to see a provider once scheduled?

## Sample Claims

[sample_claim_notes_1.md](..\sample_claim_notes\sample_claim_notes1.md)

[sample_claim_notes_2.md](..\sample_claim_notes\sample_claim_notes2.md)

## How we will evaluate

## How we will evaluate

We wil be primarily considering:

- Completeness - did you get to a working version that can answer a few queries reliably?
- Quality - is the system organized with clear components and abstractions? Are clear evolution paths supported?
- Thoughtfulness - did you make an effort to engage with unfamiliar material (claims files), understand the domain (as much as can be expected given the short timeframe), and consider scope and tradeoffs? Were you intentional about both what and what not to include?
- X factor - Feel free to include anything useful or cool that shows off how you approached the task. Vibe coded tooling, evals, a well written spec, chat logs with your coding agent, etc.

**Given you have limited time to dedicate to the exercise, prefer to show the qualities above on a limited scope vs going broad and shallow.**

## Other Notes

- The goal is to build a system that can provide trends and insights over many claims. If you only needed to look at one claim, you could answer the questions above with a simple llm call, but that is not the goal of this exercise!
- Use AI and whatever tools you want to complete the exercise
- However you build it, good coding and architectural standards still apply
- Use whatever language you want (we use Typescript), but please keep your solution simple. We should able to run this on a developer’s machine easily.

## OpenAI Key

- If you need additional tokens beyond your personal plan, you can use an API key we created for this exercise:
    - Decryption instructions below:

```
AES-256-CBC | key=SHA256("adaptional") | IV=first 16 bytes of payload | base64: SriUUolGTl3oPDyuN+eCMUj6T8nsNxETBodXNdfdqo8HKfZnUcVZpR5wNUcxpJV2S22beE0ICO6n5dxnn8OB6+d4F/ODqXqhrISeu+8gaxUfR869H+1goWW4A/0FOym6JUYYx7rvyVYE35KRF0dRCr9frokHocr2UfEdZ31GMSeyU4VkMWaBDyIIwatg3i4Al+EAQbqBmbYB3BkMnFekhPn1uhC3/bqto34DE8aACCtDjHsaZS4sucBPYcEnkkr2
```