const CONSOLE_HEADERS = {
  "Content-Type": "application/json",
  "X-ToupTek-Console": "1",
};

async function decode(response) {
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = body.error || body.description || message;
    } catch {
      // The HTTP status remains the safest fallback.
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function decodeBlob(response) {
  if (!response.ok) return decode(response);
  return response.blob();
}

export async function getJson(path, signal) {
  return decode(await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    signal,
  }));
}

export async function mutate(path, body) {
  return decode(await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: CONSOLE_HEADERS,
    body: JSON.stringify(body),
  }));
}

export async function getBlob(path, signal) {
  return decodeBlob(await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    signal,
  }));
}

export function eventStream(onState, onError) {
  const source = new EventSource("/web/v1/events");
  source.addEventListener("state", (event) => {
    try {
      onState(JSON.parse(event.data));
    } catch (error) {
      onError(error);
    }
  });
  source.onerror = () => onError(new Error("Live state stream interrupted"));
  return source;
}
