<p align="center">
  <img src="https://www.heliumedu.com/img/helium-logo.png" alt="Helium" width="300" />
  <br />
  <img src="https://www.heliumedu.com/img/og-default.png" alt="Helium - Student Planner" width="800" />
</p>

---

[**Helium**](https://www.heliumedu.com) is a free, color-coded online student planner for classes, homework, grades, and notes.

<p align="center">
  <a href="https://apps.apple.com/us/app/helium-student-planner/id6758323154"><img src="https://www.heliumedu.com/img/ios-badge.png" alt="Download on the App Store" height="50" /></a>
  &nbsp;
  <a href="https://play.google.com/store/apps/details?id=com.heliumedu.heliumapp"><img src="https://www.heliumedu.com/img/play-badge.png" alt="Get it on Google Play" height="50" /></a>
</p>

<p align="center">
  <a href="https://www.patreon.com/alexdlaird/membership"><img src="https://www.heliumedu.com/img/support-patreon.png" alt="Support on Patreon" height="30" /></a>
</p>

---

# Helium Connector

[![Build](https://img.shields.io/github/actions/workflow/status/HeliumEdu/connector/build.yml)](https://github.com/HeliumEdu/connector/actions/workflows/build.yml)
[![Code Quality](https://app.codacy.com/project/badge/Grade/111cb4a95255430da06806783ae2c16e)](https://app.codacy.com/gh/HeliumEdu/connector/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)
![GitHub License](https://img.shields.io/github/license/heliumedu/connector)

The Helium `connector` is a remote [MCP](https://modelcontextprotocol.io) server that connects AI assistants to a
student's Helium account, to import a term from syllabi and read the planner. It makes no model calls, and calls
Helium's [API](https://api.heliumedu.com/docs/) with the student's own OAuth token. The repository also holds
`heliumedu`, a typed, async Python client for that API.

## Prerequisites

- Python (>= 3.13)

## Getting Started

To install the connector and its development dependencies into a virtual environment, execute:

```sh
make install
```

To run the tests and the static checks, execute:

```sh
make test
make check
```

To serve the connector locally at `http://127.0.0.1:8001/mcp` against a local `platform`, execute:

```sh
HELIUM_API_URL=http://localhost:8000 HELIUM_CONNECTOR_URL=http://127.0.0.1:8001 make run
```

### Basic Usage

The connector is served at `https://mcp.heliumedu.com/mcp`. Add it to your AI assistant, then sign in to Helium and
choose **Allow** when it opens in your browser. For a local connector, use `http://127.0.0.1:8001/mcp` instead.

Attach your syllabi and ask your assistant to import the term, or ask about your planner, and it will use the following
tools:

| Tool                  | Access | Returns                                                                               |
|-----------------------|--------|---------------------------------------------------------------------------------------|
| `preview_import_term` | Read   | Every problem with a term's plan, or a summary to confirm, without saving anything    |
| `import_term`         | Write  | The new term's id and its classes' ids, imported all at once                          |
| `list_assignments`    | Read   | Assignments due in a window or matching text, with class, category, completion, grade |
| `list_events`         | Read   | Events in a window or matching text, with repeating events listed per occurrence      |
| `list_notes`          | Read   | Notes, optionally matching text or for one assignment, as plain text                  |
| `list_classes`        | Read   | Classes by term, with meeting times, teacher, credits, and grading categories         |
| `list_resources`      | Read   | Resources by resource group, with status, price, and the classes each is for          |
| `get_grades`          | Read   | Grades, per class and category                                                        |

#### Permissions

Signing in grants two scopes, shown on Helium's consent screen:

- `planner:read`: every read tool.
- `planner:import`: `preview_import_term` and `import_term`.

`import_term` is the only write, and none is sensitive: no tool sends messages, makes payments, or changes or deletes
anything. It adds one new term (its classes, meeting times, grading categories, assignments, and events) in a single
transaction, so a failed import saves nothing. It refuses a term whose title and dates are already in Helium unless
`allow_duplicate_term` is set. `preview_import_term` checks the same plan with Helium without saving it.

#### Inputs

- `preview_import_term`, `import_term`: `plan` (the term, its classes, and its events, with local dates and times),
  `allow_duplicate_term`.
- `list_assignments`: `start_date`, `end_date`, `class_id`, `status` (`all`, `incomplete`, `completed`, `overdue`),
  `query`, `limit` (1-100, default 25), `page`.
- `list_events`: `start_date`, `end_date`, `query`, `limit` (1-100, default 25), `page`.
- `list_notes`: `query`, `assignment_id`, `limit` (1-10, default 5), `page`.
- `list_classes`, `get_grades`: `include_past_terms`.
- `list_resources`: `class_id`.

Each tool's JSON Schema, served by the connector, describes every field. Dates and times are local to the time zone
set in the user's Helium account. A window covers at most 366 days. Lists return results a page at a time, with
`total` and `has_more`.

#### Errors and Limits

- A plan Helium can't accept returns `problems`, each naming the class, assignment, or event it's about, rather than an
  error. Details worth confirming with the student, such as a class with no credits set, return as `warnings`.
- A signed-out connection, or one not given access for a tool, returns an error asking the user to reconnect.
- Requests share the user's Helium API rate limit (130 requests per minute). Past it, the error says how many seconds
  to wait.

## Documentation

Helium's API documentation is available at [api.heliumedu.com/docs](https://api.heliumedu.com/docs/), and help with
using Helium at the [Support Portal](https://www.heliumedu.com/support/).
