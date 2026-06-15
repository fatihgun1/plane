/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { startTransition } from "react";
import { hydrateRoot } from "react-dom/client";
import { HydratedRouter } from "react-router/dom";

import polyfills from "@/lib/polyfills";

void polyfills;

// NOTE: StrictMode is intentionally disabled. In dev it double-invokes
// effects (mount → unmount → mount), which resets collapsibles like the
// "Sub-work items" panel right after they open. Removing it stops that.
startTransition(() => {
  hydrateRoot(document, <HydratedRouter />);
});
