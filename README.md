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
[![Coverage](https://img.shields.io/codecov/c/github/HeliumEdu/connector)](https://codecov.io/gh/HeliumEdu/connector)
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
choose **Allow** when it opens in your browser. For a local connector, use `http://127.0.0.1:8001/mcp` instead. For a
walkthrough, see [Use AI as a Scheduling Assistant](https://www.heliumedu.com/support/getting-started/where-to-start-with-helium/#use-ai-as-a-scheduling-assistant).

Ask your assistant to import a term from your syllabi, or about your planner, and it will use the following tools:

| Tool                     | Access | Returns                                                                               |
|--------------------------|--------|---------------------------------------------------------------------------------------|
| `preview_import_term`    | Read   | Every problem with a term's plan, or a summary to confirm, without saving anything    |
| `import_term`            | Write  | The term's id and its classes' ids: a new term, or classes added to an existing one   |
| `preview_import_ics`     | Read   | How many assignments or events an `.ics` calendar file's entries would become         |
| `import_ics`             | Write  | An `.ics` file's entries as a class's assignments, or as events, imported all at once |
| `list_assignments`       | Read   | Assignments due in a window or matching text, with class, category, completion, grade |
| `list_events`            | Read   | Events in a window or matching text, with repeating events listed per occurrence      |
| `list_notes`             | Read   | Notes as plain text, optionally matching text or for one class or linked item         |
| `list_classes`           | Read   | Classes by term, with meeting times, teacher, credits, and grading categories         |
| `list_resources`         | Read   | Resources by resource group, with status, price, and the classes each is for          |
| `get_grades`             | Read   | Grades and trends, per class and category, or the score a class needs for a grade     |

Each tool's JSON Schema, served by the connector, describes its inputs.

For assistants that offer prompts, these are listed alongside the tools:

| Prompt            | Does                                                                          |
|-------------------|-------------------------------------------------------------------------------|
| `import_syllabi`  | Sets up a term from your syllabi, asking about anything they leave unclear    |
| `plan_study_time` | Suggests what to work on with the time you have, from your planner and grades |

#### Permissions

Signing in grants the `planner:read` and `planner:import` scopes, shown on Helium's consent screen and described in
its [API documentation](https://api.heliumedu.com/docs/). `import_term` and `import_ics` are the only writes, and they
only add to Helium: no tool sends messages, makes payments, or changes or deletes anything.

### Plugin Package

`plugin/` is the connector as an [Agent Plugins](https://agent-plugins.org) package: `plugin.json`, and `mcp.json`
pointing at `https://mcp.heliumedu.com/mcp`. A plugin directory's listing, review cases, and release notes live under
its key in `plugin.json`'s `extensions`. Each release attaches it as `helium-plugin-<version>.zip`, with `version`
set to the connector's; to build it locally, execute:

```sh
make package-plugin
```

Upload a new ZIP to the plugin directory only when `plugin/` changes; tool changes reach listings from the server.

## Documentation

Helium's API documentation is available at [api.heliumedu.com/docs](https://api.heliumedu.com/docs/), and help with
using Helium at the [Support Portal](https://www.heliumedu.com/support/).
