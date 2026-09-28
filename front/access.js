const accessPanel = document.querySelector("#access-panel");
const accessForm = document.querySelector("#access-form");
const accessInput = document.querySelector("#access-code");
const accessMessage = document.querySelector("#access-message");
const accessButton = document.querySelector("#access-button");
const ACCESS_STORAGE_KEY = "lecturescribe-access-code";

let accessToken = "";
let reloadAfterUnlock = false;
let resolveReady;
const ready = new Promise((resolve) => { resolveReady = resolve; });

function savedToken() {
  try {
    return sessionStorage.getItem(ACCESS_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function saveToken(token) {
  try {
    if (token) sessionStorage.setItem(ACCESS_STORAGE_KEY, token);
    else sessionStorage.removeItem(ACCESS_STORAGE_KEY);
  } catch {
    // The current page can still use the code if browser storage is disabled.
  }
}

function accessHeaders() {
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {};
}

function showAccess(message) {
  document.body.classList.add("access-pending");
  accessPanel.classList.remove("is-hidden");
  accessForm.classList.remove("is-hidden");
  accessMessage.textContent = message;
  accessInput.focus();
}

function unlock(token) {
  accessToken = token;
  saveToken(token);
  if (reloadAfterUnlock) {
    window.location.reload();
    return;
  }
  accessPanel.classList.add("is-hidden");
  document.body.classList.remove("access-pending");
  resolveReady();
}

async function checkToken(token) {
  const response = await fetch("/auth/check", {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  return response.ok;
}

async function initializeAccess() {
  try {
    const response = await fetch("/auth/config", { cache: "no-store" });
    if (!response.ok) throw new Error("The access service is unavailable. Reload to retry.");
    const config = await response.json();
    if (!config.required) {
      unlock("");
      return;
    }
    const token = savedToken();
    if (token && await checkToken(token)) {
      unlock(token);
      return;
    }
    saveToken("");
    showAccess("Enter the access code to open LectureScribe.");
  } catch {
    showAccess("Could not check access. Please reload and try again.");
  }
}

accessForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = accessInput.value.trim();
  if (!token) {
    accessInput.focus();
    return;
  }
  accessButton.disabled = true;
  try {
    if (await checkToken(token)) {
      accessInput.value = "";
      unlock(token);
    } else {
      accessMessage.textContent = "That access code did not work.";
      accessInput.select();
    }
  } catch {
    accessMessage.textContent = "Could not check access. Please try again.";
  } finally {
    accessButton.disabled = false;
  }
});

window.lectureScribeAccess = {
  ready,
  headers: accessHeaders,
  handleUnauthorized(response) {
    if (response.status !== 401) return;
    accessToken = "";
    saveToken("");
    reloadAfterUnlock = true;
    showAccess("Your access code is no longer valid. Enter it again.");
  },
};

initializeAccess();
