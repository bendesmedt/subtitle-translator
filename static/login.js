"use strict";

ST.$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter || event.target.querySelector("button");
  button.disabled = true;
  ST.msg("login-msg", "");
  try {
    await ST.api("POST", "/api/login", {
      username: ST.$("username").value,
      password: ST.$("password").value,
    });
    window.location.href = "/";
  } catch (e) {
    ST.$("password").value = "";
    ST.msg("login-msg", e.message, "error");
  } finally {
    button.disabled = false;
  }
});
