package com.flippingengine;

import com.google.gson.JsonObject;
import java.awt.Color;
import java.awt.Graphics2D;
import java.awt.image.BufferedImage;
import javax.inject.Inject;
import lombok.extern.slf4j.Slf4j;
import net.runelite.api.Client;
import net.runelite.api.GameState;
import net.runelite.api.Player;
import net.runelite.api.events.GameStateChanged;
import net.runelite.api.events.GameTick;
import net.runelite.api.events.GrandExchangeOfferChanged;
import net.runelite.api.events.ItemContainerChanged;
import net.runelite.api.gameval.InventoryID;
import net.runelite.client.callback.ClientThread;
import net.runelite.client.config.ConfigManager;
import net.runelite.client.eventbus.Subscribe;
import net.runelite.client.plugins.Plugin;
import net.runelite.client.plugins.PluginDescriptor;
import net.runelite.client.ui.ClientToolbar;
import net.runelite.client.ui.NavigationButton;
import com.google.inject.Provides;

@Slf4j
@PluginDescriptor(
	name = "Flipping Engine",
	description = "GE flip suggestions from a self-hosted flipping-engine backend",
	tags = {"flipping", "ge", "grand exchange"}
)
public class FlippingEnginePlugin extends Plugin
{
	// ~10s between automatic suggestion refreshes (game tick = 0.6s)
	private static final int REFRESH_TICKS = 16;

	@Inject
	private Client client;
	@Inject
	private ClientThread clientThread;
	@Inject
	private ClientToolbar clientToolbar;
	@Inject
	private FlippingEnginePanel panel;
	@Inject
	private AccountStateBuilder stateBuilder;
	@Inject
	private EngineApiClient api;
	@Inject
	private OfferTracker offerTracker;

	private NavigationButton navButton;
	private boolean suggestionNeeded;
	private int lastRequestTick = -REFRESH_TICKS;
	private boolean requestInFlight;

	@Provides
	FlippingEngineConfig provideConfig(ConfigManager configManager)
	{
		return configManager.getConfig(FlippingEngineConfig.class);
	}

	@Override
	protected void startUp()
	{
		panel.setRefreshCallback(() -> clientThread.invoke(this::requestSuggestion));
		navButton = NavigationButton.builder()
			.tooltip("Flipping Engine")
			.icon(makeIcon())
			.priority(4)
			.panel(panel)
			.build();
		clientToolbar.addNavigation(navButton);
	}

	@Override
	protected void shutDown()
	{
		clientToolbar.removeNavigation(navButton);
		offerTracker.reset();
	}

	@Subscribe
	public void onGameStateChanged(GameStateChanged e)
	{
		if (e.getGameState() == GameState.LOGGED_IN)
		{
			offerTracker.reset();
			suggestionNeeded = true;
		}
	}

	@Subscribe
	public void onGrandExchangeOfferChanged(GrandExchangeOfferChanged e)
	{
		offerTracker.onOfferChanged(e.getSlot(), e.getOffer(), displayName());
		suggestionNeeded = true;
	}

	@Subscribe
	public void onItemContainerChanged(ItemContainerChanged e)
	{
		if (e.getContainerId() == InventoryID.INV)
		{
			suggestionNeeded = true;
		}
	}

	@Subscribe
	public void onGameTick(GameTick e)
	{
		boolean stale = client.getTickCount() - lastRequestTick >= REFRESH_TICKS;
		if ((suggestionNeeded || stale) && !requestInFlight && stale)
		{
			requestSuggestion();
		}
	}

	private void requestSuggestion()
	{
		if (client.getGameState() != GameState.LOGGED_IN)
		{
			return;
		}
		suggestionNeeded = false;
		lastRequestTick = client.getTickCount();
		requestInFlight = true;
		JsonObject state = stateBuilder.build();
		api.requestSuggestion(state,
			s -> {
				requestInFlight = false;
				panel.showSuggestion(s);
			},
			err -> {
				requestInFlight = false;
				panel.showError(err);
			});
	}

	private String displayName()
	{
		Player p = client.getLocalPlayer();
		return p == null ? null : p.getName();
	}

	private static BufferedImage makeIcon()
	{
		BufferedImage img = new BufferedImage(16, 16, BufferedImage.TYPE_INT_ARGB);
		Graphics2D g = img.createGraphics();
		g.setColor(new Color(0xe8, 0xb6, 0x4c));
		g.fillRoundRect(1, 1, 14, 14, 4, 4);
		g.setColor(new Color(0x16, 0x18, 0x1d));
		g.drawPolyline(new int[]{4, 7, 9, 12}, new int[]{11, 7, 9, 4}, 4);
		g.dispose();
		return img;
	}
}
