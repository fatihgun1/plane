/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
// types
import type { TPageNavigationTabs } from "@plane/types";
// components
import { ListLayout } from "@/components/core/list";
// plane web hooks
import type { EPageStoreType } from "@/plane-web/hooks/store";
import { usePageStore } from "@/plane-web/hooks/store";
// local imports
import { PageListBlock } from "./block";

type TPagesListRoot = {
  pageType: TPageNavigationTabs;
  storeType: EPageStoreType;
};

export const PagesListRoot = observer(function PagesListRoot(props: TPagesListRoot) {
  const { pageType, storeType } = props;
  // store hooks
  const { getCurrentProjectFilteredPageIdsByTab, getPageById } = usePageStore(storeType);
  // expand/collapse state per page (undefined = expanded by default)
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  // derived values
  const filteredPageIds = getCurrentProjectFilteredPageIdsByTab(pageType);

  if (!filteredPageIds) return <></>;

  // Group pages into a parent → children tree. A page is a root when it has no
  // parent, or its parent is not in the current (filtered) set — so filtering
  // never makes a page disappear, it just surfaces as a root.
  const idSet = new Set(filteredPageIds);
  const childrenByParent: Record<string, string[]> = {};
  const rootIds: string[] = [];
  for (const id of filteredPageIds) {
    const parentId = getPageById(id)?.parent;
    if (parentId && idSet.has(parentId)) {
      (childrenByParent[parentId] ??= []).push(id);
    } else {
      rootIds.push(id);
    }
  }

  const isExpanded = (id: string) => !collapsed[id];
  const toggle = (id: string) => setCollapsed((prev) => ({ ...prev, [id]: !prev[id] }));

  // Flatten the tree (depth-first), honouring expand state, into ordered rows.
  const rows: { id: string; level: number }[] = [];
  const walk = (ids: string[], level: number) => {
    for (const id of ids) {
      rows.push({ id, level });
      const children = childrenByParent[id];
      if (children?.length && isExpanded(id)) walk(children, level + 1);
    }
  };
  walk(rootIds, 0);

  return (
    <ListLayout>
      {rows.map(({ id, level }) => (
        <PageListBlock
          key={id}
          pageId={id}
          storeType={storeType}
          level={level}
          hasChildren={!!childrenByParent[id]?.length}
          isExpanded={isExpanded(id)}
          onToggleExpand={() => toggle(id)}
        />
      ))}
    </ListLayout>
  );
});
