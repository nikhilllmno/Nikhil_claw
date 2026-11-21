---
name: diagram
description: Draw architecture, flow, and sequence diagrams
version: 1.0.0
tags: [creative, diagrams, software]
---

# Diagram

Use this skill when a picture explains a system, a process, or a sequence of steps better
than text: an architecture, a data flow, a user journey, a state machine, or an org chart.

## Steps

1. Find the one question that the diagram answers, for example "how does a request reach
   the database?". One diagram answers one question.
2. Choose the type:
   - flowchart: steps and decisions, or components and their links,
   - sequence diagram: messages between parts in time order,
   - state diagram: the states of one thing and the events that change them,
   - entity relationship diagram: tables and their relations,
   - timeline or Gantt chart: work over time.
3. List the nodes and the links in text first. Keep to about 12 nodes. Split a bigger
   picture into an overview and detail diagrams.
4. Write the diagram in Mermaid. Mermaid renders in most chat apps, GitHub, and Notion.
5. When the user needs an image file, render it in the sandbox with the Mermaid command-line
   tool (`mmdc`) if it is installed, or write an SVG by hand for a simple diagram.
6. Read the diagram again and check that each link has a direction and, when useful, a
   label.

## Rules

- Use the names that the team uses for the components.
- Use short node ids and put the text in labels. Put a label in double quotes when it has
  spaces or special characters.
- Do not use color as the only way to show a meaning.
- Put a one-line title above the diagram.
