package com.flippingengine;

import net.runelite.client.config.Config;
import net.runelite.client.config.ConfigGroup;
import net.runelite.client.config.ConfigItem;

@ConfigGroup("flippingengine")
public interface FlippingEngineConfig extends Config
{
	@ConfigItem(
		keyName = "apiUrl",
		name = "Engine API URL",
		description = "Base URL of the self-hosted flipping-engine backend"
	)
	default String apiUrl()
	{
		return "http://127.0.0.1:8787";
	}

	@ConfigItem(
		keyName = "f2p",
		name = "F2P mode",
		description = "Only suggest free-to-play items and use 3 GE slots"
	)
	default boolean f2p()
	{
		return false;
	}

	@ConfigItem(
		keyName = "minProfit",
		name = "Min profit",
		description = "Minimum estimated total profit (gp) for buy suggestions"
	)
	default int minProfit()
	{
		return 0;
	}
}
