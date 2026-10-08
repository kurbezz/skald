// Review fields are a native <details> disclosure and work without JS.
// This module only guards against double submits.
export function initializeGrabSubmitGuard(root = document) {
  root.querySelectorAll("[data-grab-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("[data-grab-submit]");
      if (!button) return;
      // Disable after the submit event so the button's own form data is unaffected.
      setTimeout(() => {
        button.disabled = true;
        button.textContent = "Adding…";
      }, 0);
    });
  });
}

if (typeof document !== "undefined") initializeGrabSubmitGuard();
