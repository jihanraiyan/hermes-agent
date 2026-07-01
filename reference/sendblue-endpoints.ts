import { config } from "../config.js";

const BASE_URL = "https://api.sendblue.co";

const headers = {
  "sb-api-key-id": config.SENDBLUE_API_KEY,
  "sb-api-secret-key": config.SENDBLUE_API_SECRET,
  "Content-Type": "application/json",
};

/** iMessage expressive effects supported by send_style. */
export type SendStyle =
  | "celebration"
  | "fireworks"
  | "lasers"
  | "love"
  | "confetti"
  | "balloons"
  | "invisible"
  | "gentle"
  | "loud"
  | "slam";

async function post(path: string, body: unknown): Promise<unknown> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Sendblue ${path} failed (${res.status}): ${text}`);
  }
  return res.json().catch(() => ({}));
}

export function sendMessage(opts: {
  number: string;
  content: string;
  mediaUrl?: string;
  sendStyle?: SendStyle;
}): Promise<unknown> {
  return post("/api/send-message", {
    number: opts.number,
    from_number: config.SENDBLUE_NUMBER,
    content: opts.content,
    ...(opts.mediaUrl ? { media_url: opts.mediaUrl } : {}),
    ...(opts.sendStyle ? { send_style: opts.sendStyle } : {}),
  });
}

/** Best-effort UX niceties — never let these block or fail a reply. */
export async function sendTyping(number: string): Promise<void> {
  try {
    await post("/api/send-typing-indicator", {
      number,
      from_number: config.SENDBLUE_NUMBER,
    });
  } catch (err) {
    console.warn("typing indicator failed:", err);
  }
}

export async function markRead(number: string): Promise<void> {
  try {
    await post("/api/mark-read", {
      number,
      from_number: config.SENDBLUE_NUMBER,
    });
  } catch (err) {
    console.warn("mark-read failed:", err);
  }
}
