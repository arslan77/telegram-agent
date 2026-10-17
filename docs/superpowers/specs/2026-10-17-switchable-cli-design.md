# Switchable Cursor and Claude Code, with a setup page

17 October 2026

## The idea

The Telegram bot stays the way you talk to the machine. Under it, either Cursor or Claude Code does the work. You choose which one.

After someone clones this and installs it, a small web page opens. It asks for the Telegram details, then signs in Cursor or Claude Code. That same page is where you switch the tool later.

Claude Code means the Claude Code command-line tool from Anthropic.

## Is it feasible?

Yes. This is a small project on top of the bot you already have, not a new bot.

The bot already does the hard half for Cursor. It polls Telegram, checks the phone allowlist, queues messages, and runs the Cursor `agent` command. The live reply, the "What Cursor did" section, and the saved session all come from that one command. Claude Code is a second command of the same kind. The page is a local screen for secrets and for the switch. Neither piece needs a new language or a hosted service.

The switch and the setup page are about a week. The remote chat page takes the first version closer to two weeks. The Claude adapter is the larger risk. The pages are the smaller part.

## What you would see

You clone the repo and run the installer. A browser opens to a page on this computer only. If the machine has no screen, the installer prints the address and you open it through an SSH tunnel.

The page has three steps.

1. Telegram. You paste the bot token from BotFather and the phone numbers allowed to talk to it. The page does not create the bot. BotFather stays in Telegram.
2. Tool. You pick Cursor or Claude Code. A button starts that tool's own sign-in and shows the link it prints. You approve it in the browser. The page then shows signed in, or not.
3. Done. The bot starts answering Telegram. A switch on the page changes which tool the next message uses.

The chat in Telegram looks like it does today. The status line names the tool that is actually running.

## How it fits the code

The Cursor path stays. `scripts/login.sh` and the `agent` command stay the way they are.

Claude Code is a second runner behind the same live board. Its stream of text and tool calls is translated into the events the board already understands. The section that shows what the tool did keeps working. The heading says Claude when Claude is the one working.

Each chat remembers two sessions, one per tool. Cursor and Claude do not share a session id. Switching tools does not throw away the other session. The next message on the new tool starts from that tool's own session.

The switch is stored by the running bot and read for every new message. Changing it does not require a restart. A message already in progress finishes on the tool it started with.

The pages are served by the bot. They are not a second program. The token and the phone list stay in the same private env file they use now. The setup page writes that file. It does not put secrets in the repo.

Cursor remains the default when someone never opens the page and the env file is already filled in, which is how this machine and the Pakistan laptop work today.

`/model` keeps choosing the Cursor model. Claude Code uses its own default until a later change adds a model field for it.

## What I decided

These are my calls, so you can reject them.

- The page belongs to the bot. One process, one installer.
- The choice of tool is for the whole machine, not a separate choice in every Telegram chat.
- Switching applies to the next message. It does not move a half-finished Cursor chat into Claude.
- Sign-in stays the vendor's own login. The page starts it and shows the link. It does not ask for a Cursor or Claude password.
- The first version switches the tool only. It does not grow a model picker for Claude.
- The remote chat is the same Python bot, reached over Tailscale. It is not a public website, and it is not a Telegram Mini App.

## What would make it fail

It fails if a switch is expected to continue the same conversation inside the other tool. The transcript over there does not exist.

It fails if the setup page is opened to the public internet. Anyone who could reach it could paste a token. The setup page stays on localhost. The chat page is only on the tailnet.

It fails if the machine has no browser and nobody can open the printed address. The installer has to print that address. The headless machines you already use can keep the env file and skip the page.

It fails if Claude's stream format and Cursor's stream format are treated as the same JSON. They are not. The adapter has to translate. Claude Code is not installed on this computer yet, so that translation has to be checked against a real `claude` login before the switch is offered as done.

## What the first version does not do

It does not run both tools on one message. It does not add a third tool. It does not change the phone allowlist rules, the queue, or the rich-message behavior. It does not publish a website on the public internet.

## A remote conversational screen

You also want a conversational web UI, reachable when you are not sitting at the machine. Two shapes were on the table: a Telegram web app, or a separate Python application. Both can be built. One of them fits this bot.

### Telegram already is the remote chat

The phone already talks to the machine through the bot. A second chat has to earn its place. The reason for a web chat is a browser: a wider thread, the setup page and the switch in the same place, and a way in when you want the screen rather than the Telegram app.

### A Telegram Mini App

A Mini App is a web page Telegram opens inside the chat, from a button. Telegram requires that page to be HTTPS. The page is loaded by the phone, not by Telegram's servers.

It can work. The phone is already on the tailnet, and Tailscale Serve can provide the HTTPS certificate. The page would still call this bot for every message. The Mini App does not run the agent by itself.

It is a weak fit for the conversation. Telegram is already that conversation, with replies, voice notes, and files. A page inside Telegram would repeat it, and it would still need the HTTPS address, the signed Telegram login check, and a phone that trusts that certificate. It is a reasonable place for a button that opens the switch. It is a poor place for the thread.

### A Python web chat

This is the one that fits. The bot you already run grows a chat page. There is no second application and no new language. A message typed in the browser enters the same queue as a Telegram message. The same Cursor or Claude Code session answers it. The page shows the same live status the Telegram message already edits.

Remote access uses the tailnet you already have. This machine is `alienware-lab`, the Pakistan laptop is `redcode`, and the phone is on that tailnet too. The chat listens on the tailnet address only. It does not listen on the public internet. No static IP and no open port on the router.

The setup page stays on localhost. That is the page that accepts the bot token. The chat page is the one reached from the phone or the other laptop.

Who can open the chat: a device on this tailnet. That is the same set of machines you already approved. A stranger on the internet cannot open it.

### What I would build

One process. Two pages.

- `http://127.0.0.1:<port>/` for setup and the tool switch. This never leaves the machine.
- `http://<tailnet-address>:<port>/chat` for the conversation. Tailscale is the lock.

The first version of the chat is plain. You type, the reply streams, you see which tool is working. Voice notes and file uploads stay in Telegram until a later version. The browser chat and the Telegram chat share the queue, so one does not run over the other.

Adding this to the first version makes it closer to two weeks than one. The Claude adapter is still the larger risk. The chat page is ordinary web work once the bot can accept a message from somewhere other than Telegram.
