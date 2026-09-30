# ReviewLens AI – Original Brief

This is the source brief for the project, kept for reference. Decisions made after it are listed at the end; where they differ from the brief, the decisions win.

## Business Context

Imagine a consultancy that specializes in Online Reputation Management (ORM). Their business model relies on analyzing massive amounts of fragmented customer feedback to offer strategic services to brands.

Currently, their analysts spend hours manually reading reviews to identify "pain points." They need a rapid prototype of a Review Intelligence Portal that can ingest a product's digital footprint and allow an analyst to "talk" to that data to find specific trends—without the AI drifting into generalities or competitor data.

## The Mission

Develop ReviewLens AI: A secure, web-based portal that enables a user to track a product or entity from a single review platform (Amazon, Google Maps, G2, Capterra, or a similar publicly accessible platform) and analyze those reviews using a guardrailed Q&A interface.

Whichever platform you choose, it should:

- Be publicly accessible — open to browse without authentication
- Feature user-generated content — customer-written text reviews and ratings
- Ready to be deployed to production—build the system such that we can deploy to production

## Our Review Approach

We value your ability to build great software, including your use of AI tools (Claude Code, Cursor, Codex, Copilot, etc.), which is expected.

As we review this project, we'll focus on the professional quality and speed of delivery, the judgment you use when working with AI, and the engineering instincts you demonstrate—all vital traits for a senior member of our team.

## Core Requirements

### 1. Ingestion & Scraping Result Summary

- **Ingestion Module:** The application should accept a target URL from the chosen platform and extract the relevant review data, or otherwise allow the user to supply the data in a practical format for analysis.
- **Ingestion Result Summary:** Give the user a clear summary of what was successfully ingested — whether that's text-based, tabular, or a visual. The goal is to give confidence that the data is accurate, sufficiently complete, and ready for analysis.

### 2. Guardrailed Q&A Interface

- **Interactive Chat:** Build a user-facing interface where users can pose questions exclusively about the ingested reviews.
- **Scope Guard Enforcement:** This is one we care a lot about. If a user asks about an external platform or general world knowledge, the AI should gracefully and explicitly decline (e.g., if tracking Google Maps, it shouldn't discuss Amazon reviews or the current weather). This should be primarily driven by your system prompt configuration.

### 3. Deployment

- **Hosting:** We'd like to see the application hosted publicly and accessible via a URL.
- **Code:** Please share the full source code in a GitHub Repository.

## Technical Notes

Hosting platform considerations. We need this to be inexpensive and easy to deploy to as well as integrate API calls to the AI elements. The UI will be web based and should work as fast as possible on a reasonable set of data. The configuration and AI data storage will have to be determined by the API selected but I suspect hosting on AWS or Google might offer some performance and cost advantages. There are hosting plans out there that have free tiers but I suspect the ability to integrate AI API calls might be limited.

There are two primary interfaces that need to be built:

1. The selection/addition of review data sets. These might come from a specific URL that points to a product or service for which has reviews available. It might be nice to allow for an upload of a CSV or tabular text data which contains the review details but that could also be an acceptable form of URL destination. The processed data sets will appear in a list and allow for archiving, opening, or updating. This encompasses the "Ingestion Module" and small parts of the "ingestion summary".
2. After a data set selection is made an interactive chat window appears along with some general data about the data set (the complete Ingestion Summary). This might include number of entries, general sentiment, name of data set, url, date of last update. The chat window will prompt the user to ask questions of the review data and keep the answers only to that data (Guardrailed Q&A Interface).

## Data processing

In the Ingestion Module, when a URL is requested and tested to be valid, the engine will store the page title, request date, last update date, initial URL string, status set to "requested", and a unique ID into a data store like an RDBMS. This data store will have a json 'status_detail' field that contains detailed status information of the processing with status and timestamps of key processing events. The scraped web data will be stored in S3 file referenced by the unique id and also a snapshot of the top part of the page in PNG using the same id (this is to be displayed as part of the "ingestion summary"). No entry will be stored if the URL is invalid and will return as not found in fact any non 200 return will not be stored but rejected immediately. All redirect routes will be followed and the final URL that returns a 200 will be used but the original URL requested will still be the main URL displayed. Details of the redirects will be logged in the status_detail column.

A saved row with a 'requested' status will trigger a data processing step using the S3 data that looks to analyze the review data found in the page data (the data might have been across multiple pages, how many is the max?) an AI process will be trained to look only at the review data and the basic assumptions about what is being reviewed based solely on the S3 content. No other data will be loaded into the analysis and guardrails must be established to keep answers relevant to the reviews and the product/service itself. When this process begins the status of the row should be set to "processing". When the processing is complete the information of the processing should be updated in the database and the status should be set to "updated" with the updated date being set. The status and details should be pushed to the site so that anyone looking at the list of data sets or is in the detail page that matches the updated data set would be dynamically updated.

The session of questions and answers should be saved and will persist across sessions of use. One way to keep this data is to store a log of requests and responses in a file and save to S3 using the ID. When a data set is pulled up the history is also pulled up and displayed above the chat window so the user can scroll through previous conversations that relate to this entry. We can also update the history when the question is answered so that only questions are allowed in the chat dialog box.

Tests need to be built to test each part of the code and also tests the integrated code.

## Decisions made after the brief

1. Users provide the URLs. The app tells them which URLs will work, based on analysis of the returned HTML, before they're added.
2. The UI clearly separates where to request a new URL from the list of previously submitted URLs.
3. A URL is never tracked twice; submitting it again refreshes the original dataset.
4. The Q&A log shows when the data was refreshed, so users know when earlier answers may no longer apply.
5. There is no sign-in or user identity. Everyone sees the same datasets and history.
6. Every service must run on Lambda or in containers so horizontal scaling is easy later.
7. Review detection should be dynamic and AI-first where AI works better.
8. The database is Aurora PostgreSQL Serverless v2.
