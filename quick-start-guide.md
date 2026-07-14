# Odoo Sidekick — Quick Start Guide

*Get answers from your Odoo data by asking Claude in plain English.*

**Version covered:** 1.8 &nbsp;·&nbsp; **Last updated:** 2026-07-14

Instead of clicking through Odoo's menus, filters, and saved reports, you can just ask. "What did we sell last month?" "Which invoices are overdue?" "What's low on stock?" Claude reads your Odoo data live and answers in seconds, in plain language, in whatever format is actually useful to you. This guide covers how to get set up, what to ask, and how to get the most useful answers out of it. Read it once, keep it nearby for the first few weeks.

*(Looking for the full technical reference — safety internals, automation, every command? See `technical-manual.md` instead. This guide is for everyday use.)*

## Getting Started

If your company has already connected Claude to your Odoo, you don't need to do anything, just start asking questions.

If this is the first time anyone has used it, Claude will walk you through a short setup, usually a couple of minutes:

- **Whether Claude can only look, or also make changes.** Most people start with look-only access. That's plenty for questions, reports, and analysis.
- **Your Odoo web address**, e.g. `https://yourcompany.odoo.com`.
- **A connection key**, which you generate inside Odoo itself (a few clicks under your profile settings) and hand off once. Claude will walk you through exactly where to find it.

Once that's done, it's set up for good, and you (or your teammates) can just start asking.

Not sure whether this is already configured at your company? Ask whoever manages your Odoo or IT setup before assuming you need to set it up yourself.

## What You Can Ask

Ask in your own words, the way you'd ask a knowledgeable coworker. A few starting points by area:

#### Sales & Customers

- "Who are our top 10 customers this year by revenue?"
- "What did we sell last month, broken down by product category?"
- "Show me all quotes that haven't been confirmed in the last two weeks."
- "Compare this quarter's sales to the same quarter last year."
- "Which customers haven't placed an order in six months?"

#### Finance & Accounting

- "Give me an accounts receivable aging report."
- "What's our total revenue and expenses for last month?"
- "Which invoices are more than 60 days overdue?"
- "Show me all unpaid vendor bills due in the next two weeks."

#### Inventory & Operations

- "What products are we running low on right now?"
- "Show me manufacturing orders scheduled for next week."
- "Which products haven't sold in the last 90 days?"
- "List our open purchase orders and their expected delivery dates."

#### For Managers & Leadership

- "Give me a one-page business health summary for this month."
- "What should I be paying attention to right now?"
- "Which team or product line is over- or under-performing versus plan?"
- "Pull together the numbers I'd need for Monday's leadership meeting."

#### Anything Cross-Functional

- "Put that in a table I can copy into a spreadsheet."
- "Give me the headline number, then break it down by product/region/rep."
- "Compare our top 5 customers' performance year over year."

If you're not sure what's possible, just ask: "What kinds of questions can you answer about our Odoo?" or describe the *decision* you're trying to make ("I need to know if we can promise a customer a delivery date") rather than the exact report you'd normally run. Claude will figure out the right data to pull, often combining information that would otherwise mean running two or three separate Odoo reports and matching them up yourself.

## Getting Great Answers

A few habits make a real difference:

- **Be specific about time and scope.** "Last month" vs. "this quarter" vs. "year to date" changes the answer. Name a specific product line, region, or customer if you have one in mind.
- **Say what format you want.** "As a table," "just the top 5," "something I can paste into Excel" all work.
- **Ask follow-ups instead of starting over.** Claude remembers the conversation, so "now break that down by region" builds on what you just saw, rather than making you re-explain everything.
- **Sanity-check surprising numbers**, at least the first few times, by asking Claude to double-check or explain how it got a figure, until you trust it the way you trust a report you already know well.
- **Ask for the latest data** if something feels out of date. Claude will sometimes work from a locally cached copy of your data for speed. Just say "get me the current numbers" or "refresh that" and it will pull live from Odoo.
- **If you ask the same question every week** (a Monday sales recap, a cash position check), just ask again each time, it runs fresh against real data every time. A fully automatic version of this is on the roadmap; ask your admin whether that's been set up separately in the meantime.
- **You don't need to know Odoo's field names or menu structure.** Describe what you want in normal business language; Claude translates that into the right query for you.

## Changes and Safety

Most setups are look-only: Claude can read and analyze anything you can see in Odoo, but cannot create, edit, or delete anything. That alone covers the large majority of what most people use this for.

If your company has deliberately turned on the ability to make changes (updating a contact, confirming an order, and similar), a few rules always apply:

- **Nothing happens silently.** Before any change, Claude lays out exactly what it's about to do, in plain language, and asks you to say yes first.
- **Your "yes" covers only what you just approved.** A new request gets a new plan and a new yes, even later in the same conversation.
- **Anything hard to undo is called out clearly**, deletions especially, before you're asked to approve it.

If you're ever unsure whether something you asked for will change data in Odoo, just ask Claude directly: "will this change anything, or is it just a report?"

## If Something Goes Wrong

Tell Claude what happened, in your own words, "that gave me an error" or "this number looks wrong." It will usually explain what happened and suggest what to try next.

If you're still stuck, or if it seems like the connection to Odoo itself needs attention, contact whoever manages Odoo or IT at your company.

## Getting Help

Odoo Sidekick is built and maintained by **SHIFTcollective**, an AI-first consultancy.

- Email: [info@shiftcollective.co](mailto:info@shiftcollective.co)
- Web: [shiftcollective.co](https://shiftcollective.co)

If you're the one setting this up for your company, or want the full technical reference, see `technical-manual.md` in the same place you found this guide.

---

*Odoo Sidekick is built and maintained by [SHIFTcollective](https://shiftcollective.co).*
