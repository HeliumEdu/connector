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

| Tool                  | Returns                                                                               |
|-----------------------|---------------------------------------------------------------------------------------|
| `preview_import_term` | Every problem with a term's plan, or a summary to confirm, without saving anything    |
| `import_term`         | The new term, imported all at once; a duplicate term is refused unless told otherwise |
| `list_assignments`    | Assignments due in a window or matching text, with class, category, completion, grade |
| `list_events`         | Events in a window or matching text, with repeating events listed per occurrence      |
| `list_notes`          | Notes, optionally matching text or for one assignment, as plain text                  |
| `list_classes`        | Classes by term, with meeting times, teacher, credits, and grading categories         |
| `list_resources`      | Resources by resource group, with status, price, and the classes each is for          |
| `get_grades`          | Grades, per class and category                                                        |

`import_term` is the only tool that writes, and it only adds a new term: no tool changes or deletes anything.

The assignment, event, and note lists return results a page at a time (`limit` and `page`), with `total` and
`has_more`.

## Documentation

Helium's API documentation is available at [api.heliumedu.com/docs](https://api.heliumedu.com/docs/), and help with
using Helium at the [Support Portal](https://www.heliumedu.com/support/).
