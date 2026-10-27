// Forms with data-busy: on submit, lock their buttons and show the busy text, so a slow (paid) call is not sent twice.
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || form.dataset.busy === undefined) return;
  if (form.dataset.sent) { event.preventDefault(); return; }
  form.dataset.sent = "1";
  // after the browser has read the form: a button that is disabled now would not send its name and value
  setTimeout(() => {
    for (const button of form.querySelectorAll("button")) {
      button.dataset.label = button.textContent;
      button.textContent = form.dataset.busy;
      button.disabled = true;
    }
  });
});
// Back to a page from the browser cache: unlock its forms again.
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  for (const form of document.querySelectorAll("form[data-sent]")) {
    delete form.dataset.sent;
    for (const button of form.querySelectorAll("button[data-label]")) {
      button.textContent = button.dataset.label;
      button.disabled = false;
    }
  }
});
// A page with data-refresh="N" (on <body>) reloads every N seconds, but not while a text field or a file field holds
// input or has the focus: a reload would throw away what the user typed or chose.
(() => {
  const seconds = Number(document.body.dataset.refresh);
  if (!(seconds > 0)) return;
  const holdsInput = (field) => field === document.activeElement
    || (field.type === "file" ? field.files.length > 0 : field.value !== field.defaultValue);
  setInterval(() => {
    const fields = document.querySelectorAll(
      "textarea, input:not([type=hidden], [type=submit], [type=button], [type=checkbox], [type=radio])");
    if (!Array.from(fields).some(holdsInput)) location.reload();
  }, seconds * 1000);
})();
