package com.flippingengine;

import net.runelite.client.RuneLite;
import net.runelite.client.externalplugins.ExternalPluginManager;

public class FlippingEnginePluginTest
{
	public static void main(String[] args) throws Exception
	{
		ExternalPluginManager.loadBuiltin(FlippingEnginePlugin.class);
		RuneLite.main(args);
	}
}
