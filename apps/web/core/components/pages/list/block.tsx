/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useRef } from "react";
import { observer } from "mobx-react";
import { ChevronRight } from "lucide-react";
import { Logo } from "@plane/propel/emoji-icon-picker";
import { PageIcon } from "@plane/propel/icons";
// plane imports
import { cn, getPageName } from "@plane/utils";
// components
import { ListItem } from "@/components/core/list";
import { BlockItemAction } from "@/components/pages/list/block-item-action";
// hooks
import { usePlatformOS } from "@/hooks/use-platform-os";
// plane web hooks
import type { EPageStoreType } from "@/plane-web/hooks/store";
import { usePage } from "@/plane-web/hooks/store";

type TPageListBlock = {
  pageId: string;
  storeType: EPageStoreType;
  level?: number;
  hasChildren?: boolean;
  isExpanded?: boolean;
  onToggleExpand?: () => void;
};

export const PageListBlock = observer(function PageListBlock(props: TPageListBlock) {
  const { pageId, storeType, level = 0, hasChildren = false, isExpanded = false, onToggleExpand } = props;
  // refs
  const parentRef = useRef(null);
  // hooks
  const page = usePage({
    pageId,
    storeType,
  });
  const { isMobile } = usePlatformOS();
  // handle page check
  if (!page) return null;
  // derived values
  const { name, logo_props, getRedirectionLink } = page;

  return (
    <ListItem
      prependTitleElement={
        <span className="flex items-center" style={{ paddingLeft: level * 22 }}>
          {hasChildren ? (
            <button
              type="button"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onToggleExpand?.();
              }}
              className="mr-1 grid size-4 flex-shrink-0 place-items-center rounded text-tertiary hover:bg-layer-transparent-hover hover:text-secondary"
              aria-label={isExpanded ? "Collapse" : "Expand"}
            >
              <ChevronRight className={cn("size-3.5 transition-transform", { "rotate-90": isExpanded })} />
            </button>
          ) : (
            <span className="mr-1 size-4 flex-shrink-0" />
          )}
          {logo_props?.in_use ? (
            <Logo logo={logo_props} size={16} type="lucide" />
          ) : (
            <PageIcon className="h-4 w-4 text-tertiary" />
          )}
        </span>
      }
      title={getPageName(name)}
      itemLink={getRedirectionLink()}
      actionableItems={<BlockItemAction page={page} parentRef={parentRef} storeType={storeType} />}
      isMobile={isMobile}
      parentRef={parentRef}
    />
  );
});
