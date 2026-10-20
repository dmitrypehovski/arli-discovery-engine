# Deploy pieces served by the estate

These files live in the repository so they are versioned, and are copied to the place that serves
them. The copy step is deliberate: a file served from a directory nobody versions is a file nobody
can review.

| file | served at | copy to |
|---|---|---|
| `install-agent.sh` | `https://arliwork.com/install-agent.sh` | `/llm-gateway/landing/install-agent.sh` |

`install-agent.sh` is the one line a user pastes to give an agent the ability to join: it installs the
engine under `~/.arli-match`, starts the agent with the user's list, waits until the card answers at a
public address, then connects and prints the three checks. It is served as `text/plain` by a dedicated
nginx location so it reads as a script rather than downloading as a binary.
