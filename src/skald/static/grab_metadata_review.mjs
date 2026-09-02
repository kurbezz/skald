export function initializeGrabMetadataReview(root = document) {
  root.querySelectorAll("[data-grab-review-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const review = button.closest("[data-grab-review]");
      const fields = review?.querySelector("[data-grab-review-fields]");
      if (!fields) return;

      fields.hidden = false;
      button.setAttribute("aria-expanded", "true");
      fields.querySelector('[name="title"]')?.focus();
    });
  });
}

if (typeof document !== "undefined") initializeGrabMetadataReview();
