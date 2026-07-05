package com.flippingengine;

import com.google.gson.JsonObject;
import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Font;
import javax.inject.Inject;
import javax.inject.Singleton;
import javax.swing.BorderFactory;
import javax.swing.BoxLayout;
import javax.swing.JButton;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JTextArea;
import javax.swing.SwingUtilities;
import net.runelite.client.ui.ColorScheme;
import net.runelite.client.ui.PluginPanel;

@Singleton
public class FlippingEnginePanel extends PluginPanel
{
	private static final Color BUY = new Color(0x7b, 0xc9, 0x6f);
	private static final Color SELL = new Color(0x58, 0xa6, 0xff);
	private static final Color ABORT = new Color(0xe0, 0x6c, 0x75);
	private static final Color WAIT = new Color(0x8b, 0x91, 0x9c);

	private final JLabel actionLabel = new JLabel(" ");
	private final JTextArea messageArea = new JTextArea(3, 20);
	private final JLabel detailsLabel = new JLabel(" ");
	private final JLabel statusLabel = new JLabel("Waiting for game data…");

	private Runnable refreshCallback = () -> {};

	@Inject
	public FlippingEnginePanel()
	{
		setLayout(new BorderLayout(0, 8));
		setBorder(BorderFactory.createEmptyBorder(10, 10, 10, 10));
		setBackground(ColorScheme.DARK_GRAY_COLOR);

		JPanel content = new JPanel();
		content.setLayout(new BoxLayout(content, BoxLayout.Y_AXIS));
		content.setBackground(ColorScheme.DARK_GRAY_COLOR);

		JLabel title = new JLabel("Flipping Engine");
		title.setFont(title.getFont().deriveFont(Font.BOLD, 15f));
		title.setForeground(new Color(0xe8, 0xb6, 0x4c));

		actionLabel.setFont(actionLabel.getFont().deriveFont(Font.BOLD, 18f));

		messageArea.setEditable(false);
		messageArea.setLineWrap(true);
		messageArea.setWrapStyleWord(true);
		messageArea.setBackground(ColorScheme.DARKER_GRAY_COLOR);
		messageArea.setForeground(Color.WHITE);
		messageArea.setBorder(BorderFactory.createEmptyBorder(6, 8, 6, 8));

		detailsLabel.setForeground(WAIT);
		statusLabel.setForeground(WAIT);
		statusLabel.setFont(statusLabel.getFont().deriveFont(11f));

		JButton refresh = new JButton("Refresh suggestion");
		refresh.addActionListener(e -> refreshCallback.run());

		content.add(title);
		content.add(javax.swing.Box.createVerticalStrut(10));
		content.add(actionLabel);
		content.add(javax.swing.Box.createVerticalStrut(6));
		content.add(messageArea);
		content.add(javax.swing.Box.createVerticalStrut(6));
		content.add(detailsLabel);
		content.add(javax.swing.Box.createVerticalStrut(10));
		content.add(refresh);
		content.add(javax.swing.Box.createVerticalStrut(10));
		content.add(statusLabel);

		add(content, BorderLayout.NORTH);
	}

	public void setRefreshCallback(Runnable r)
	{
		refreshCallback = r;
	}

	public void showSuggestion(JsonObject s)
	{
		SwingUtilities.invokeLater(() -> {
			String type = s.has("type") ? s.get("type").getAsString() : "?";
			actionLabel.setText(type.toUpperCase());
			actionLabel.setForeground(colorFor(type));
			messageArea.setText(s.has("message") ? s.get("message").getAsString() : "");

			StringBuilder details = new StringBuilder("<html>");
			if (s.has("expected_profit") && !s.get("expected_profit").isJsonNull())
			{
				details.append("Expected profit: ").append(gp(s.get("expected_profit").getAsLong())).append("<br>");
			}
			if (s.has("expected_duration_minutes") && !s.get("expected_duration_minutes").isJsonNull())
			{
				details.append("Est. roundtrip: ").append(Math.round(s.get("expected_duration_minutes").getAsDouble())).append(" min<br>");
			}
			if (s.has("fill_rate") && !s.get("fill_rate").isJsonNull())
			{
				details.append("Measured fill rate: ").append(Math.round(s.get("fill_rate").getAsDouble() * 100)).append("%");
			}
			details.append("</html>");
			detailsLabel.setText(details.toString());
			statusLabel.setText("Updated " + java.time.LocalTime.now().withNano(0));
		});
	}

	public void showError(String error)
	{
		SwingUtilities.invokeLater(() -> {
			statusLabel.setText(error);
			statusLabel.setForeground(ABORT);
		});
	}

	private static Color colorFor(String type)
	{
		switch (type)
		{
			case "buy": return BUY;
			case "sell": return SELL;
			case "abort": return ABORT;
			default: return WAIT;
		}
	}

	private static String gp(long n)
	{
		if (Math.abs(n) >= 1_000_000)
		{
			return String.format("%.2fm", n / 1_000_000.0);
		}
		if (Math.abs(n) >= 10_000)
		{
			return String.format("%.1fk", n / 1_000.0);
		}
		return String.valueOf(n);
	}
}
