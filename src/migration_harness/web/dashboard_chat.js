"use strict";

function junieChatBubble(sender, text, outgoing = false) {
  const bubble = document.createElement("div");
  bubble.className = "junie-chat-bubble";
  bubble.dataset.side = outgoing ? "outgoing" : "incoming";
  const label = document.createElement("span");
  label.className = "junie-chat-sender";
  label.textContent = sender;
  bubble.append(label);
  if (text !== undefined) {
    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    bubble.append(paragraph);
  }
  return bubble;
}

function bindJunieChat(feed, latest, form, input, send) {
  const atBottom = () => feed.scrollHeight - feed.clientHeight - feed.scrollTop <= 32;
  const sync = () => { latest.hidden = atBottom(); };
  feed.addEventListener("scroll", sync, {passive: true});
  feed.addEventListener("toggle", sync, true);
  new ResizeObserver(sync).observe(feed);
  latest.addEventListener("click", () => {
    feed.scrollTop = feed.scrollHeight;
    feed.focus({preventScroll: true});
    sync();
  });
  let composing = false;
  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend", () => { composing = false; });
  input.addEventListener("keydown", event => {
    if (event.key !== "Enter" || !event.ctrlKey || event.shiftKey || event.altKey || event.metaKey || event.repeat ||
        event.isComposing || event.keyCode === 229 || composing || input.disabled || send.disabled) return;
    event.preventDefault();
    form.requestSubmit(send);
  });
  return (update, reset = false) => {
    const follow = reset || atBottom();
    update();
    if (feed.clientHeight > 0 && follow) feed.scrollTop = feed.scrollHeight;
    sync();
  };
}
