import type { TFunction } from "i18next";

import { channelTranslator } from "@/channel-plugins/i18n";
import { channelUiOwner, channelUiPresentation } from "@/channel-plugins/registry";

export function isTopicChannel(channel: string): boolean {
  const owner = channelUiOwner(channel.split(".", 1)[0]);
  return owner === "feishu" || owner === "weixin";
}

export function topicChannelName(channel: string, t: TFunction): string {
  const base = channel.split(".", 1)[0];
  const presentation = channelUiPresentation(base);
  if (!presentation) return base;
  return channelTranslator(t, channelUiOwner(base))("displayName", presentation.displayName);
}
